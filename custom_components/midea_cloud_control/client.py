"""Cloud-relayed control for Midea-protocol air conditioners."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import aiohttp
from midealocal.cloud import SUPPORTED_CLOUDS, get_midea_cloud
from midealocal.exceptions import MideaCloudError
from msmart.device.AC.command import (
    GetStateCommand,
    InvalidResponseException,
    Response,
    SetStateCommand,
    StateResponse,
)
from msmart.frame import InvalidFrameException

from .protocol import TransparentSendDecodeError, transparent_send

_LOGGER = logging.getLogger(__name__)

SUPPORTED = ("Ariston Clima", "NetHome Plus", "Midea Air")
_SESSION_ERROR_CODES = frozenset({3106, 3144})
_LOGIN_TRANSIENT_ERROR_CODES = frozenset({9999})
_REQUEST_TRANSIENT_ERROR_CODES = frozenset({3176, 9999})
_INVALID_AUTH_CODES = frozenset({3101, 3102, 3301})
_DEFAULT_MAX_CONCURRENT_REQUESTS = 1
_DEFAULT_MIN_REQUEST_INTERVAL = 0.75
_APPLIANCE_LIST_ENDPOINT = "/v1/appliance/user/list/get"
_SN8_MIN_SERIAL_LENGTH = 17

_CARRIED_STATE = (
    "power_on",
    "target_temperature",
    "operational_mode",
    "fan_speed",
    "eco",
    "swing_mode",
    "turbo",
    "fahrenheit",
    "sleep",
    "freeze_protection",
    "follow_me",
    "purifier",
    "target_humidity",
    "aux_heat",
    "independent_aux_heat",
)

_T = TypeVar("_T")


class CloudError(Exception):
    """Base error raised by this integration's cloud layer."""


class CloudAuthenticationError(CloudError):
    """Raised after a bounded authentication failure."""


class CloudInvalidAuthError(CloudAuthenticationError):
    """Raised when credentials or the selected cloud are rejected."""


class CloudUnavailableError(CloudError):
    """Raised after bounded transient request failures."""


class CloudCooldownError(CloudUnavailableError):
    """Raised while login cooldown is active."""


class CloudProtocolError(CloudError):
    """Raised for an invalid or unexpected appliance response."""


class CloudCommandUncertainError(CloudError):
    """Raised when a command may have run but its state cannot be verified."""


class _TransientRequestError(Exception):
    """Internal marker for retryable empty/network responses."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry and cooldown settings."""

    login_attempts: int = 3
    # midea-local already retries each HTTP call up to three times. Two outer
    # attempts keep recovery bounded while still handling an empty response.
    request_attempts: int = 2
    status_attempts: int = 2
    command_verification_attempts: int = 2
    status_retry_delay: float = 3.0
    status_retry_jitter: float = 2.0
    base_delay: float = 1.0
    maximum_delay: float = 30.0
    jitter: float = 0.25
    cooldown: float = 60.0


@dataclass(slots=True)
class ApplianceMetrics:
    """Non-sensitive runtime counters for one appliance."""

    status_requests: int = 0
    status_attempts: int = 0
    command_requests: int = 0
    command_attempts: int = 0
    missing_replies: int = 0
    protocol_errors: int = 0
    total_status_seconds: float = 0.0
    last_status_seconds: float | None = None
    online: bool | None = None


class MideaCloudClient:
    """Control appliances through one resilient, shared vendor-cloud session."""

    def __init__(
        self,
        account: str,
        password: str,
        cloud_name: str = "Ariston Clima",
        session: aiohttp.ClientSession | None = None,
        *,
        max_concurrent_requests: int = _DEFAULT_MAX_CONCURRENT_REQUESTS,
        min_request_interval: float = _DEFAULT_MIN_REQUEST_INTERVAL,
        retry_policy: RetryPolicy | None = None,
        cloud_factory: Callable[..., Any] = get_midea_cloud,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if cloud_name not in SUPPORTED:
            raise ValueError(
                f"{cloud_name!r} does not serve the transparent-send endpoint; "
                f"expected one of {SUPPORTED}"
            )
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be at least 1")
        if min_request_interval < 0:
            raise ValueError("min_request_interval cannot be negative")

        self._account = account
        self._password = password
        self._cloud_name = cloud_name
        self._app_key = str(SUPPORTED_CLOUDS[cloud_name]["app_key"])
        self._session = session
        self._owns_session = session is None
        self._cloud_factory = cloud_factory
        self._validate_private_api = cloud_factory is get_midea_cloud
        self._cloud: Any = None

        self._policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._random = random_value
        self._auth_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._rate_lock = asyncio.Lock()
        self._device_locks: dict[int, asyncio.Lock] = {}
        self._metrics: dict[int, ApplianceMetrics] = {}
        self._min_request_interval = min_request_interval
        self._next_request_at = 0.0
        self._generation = 0
        self._cooldown_until = 0.0

    async def __aenter__(self) -> MideaCloudClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def generation(self) -> int:
        """Return the successful authentication generation for diagnostics/tests."""
        return self._generation

    async def connect(self) -> None:
        """Authenticate once; concurrent callers share the same login attempt."""
        if self._cloud is not None:
            return
        async with self._auth_lock:
            if self._cloud is not None:
                return
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        now = self._monotonic()
        if now < self._cooldown_until:
            raise CloudCooldownError("Cloud login is temporarily in cooldown")

        last_code: int | None = None
        last_error: Exception | None = None
        for attempt in range(self._policy.login_attempts):
            if self._session is None:
                self._session = aiohttp.ClientSession()

            # MideaAirCloud adds sessionId to future requests once authenticated.
            # A fresh object is required so an expired ID cannot poison relogin.
            candidate = self._cloud_factory(
                self._cloud_name, self._session, self._account, self._password
            )
            if self._validate_private_api:
                self._validate_cloud_private_api(candidate)
            try:
                logged_in = await candidate.login()
            except MideaCloudError as err:
                last_error = err
                last_code = err.code
                if err.code not in _SESSION_ERROR_CODES | _LOGIN_TRANSIENT_ERROR_CODES:
                    if err.code in _INVALID_AUTH_CODES:
                        raise CloudInvalidAuthError(
                            f"Cloud credentials were rejected with code {err.code}"
                        ) from err
                    self._cooldown_until = self._monotonic() + self._policy.cooldown
                    raise CloudAuthenticationError(
                        f"Cloud login was rejected with code {err.code}"
                    ) from err
                logged_in = False
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                last_error = err
                logged_in = False

            if logged_in:
                self._cloud = candidate
                self._generation += 1
                self._cooldown_until = 0.0
                _LOGGER.debug(
                    "Authenticated shared %s cloud session (generation %s)",
                    self._cloud_name,
                    self._generation,
                )
                return

            if attempt < self._policy.login_attempts - 1:
                await self._backoff(attempt)

        self._cloud = None
        self._cooldown_until = self._monotonic() + self._policy.cooldown
        detail = f" code {last_code}" if last_code is not None else ""
        error = CloudAuthenticationError(
            f"Cloud login failed after {self._policy.login_attempts} attempts{detail}"
        )
        if last_error is not None:
            raise error from last_error
        raise error

    async def close(self) -> None:
        """Discard authentication state and close only self-owned HTTP sessions."""
        async with self._auth_lock:
            self._cloud = None
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None

    async def update_password(self, password: str) -> None:
        """Replace credentials and invalidate authentication without logging in."""
        async with self._auth_lock:
            self._password = password
            self._cloud = None
            self._cooldown_until = 0.0

    async def list_appliances(self) -> dict[int, dict[str, Any]]:
        """List all appliances visible to the shared account."""
        result = await self._execute(self._list_appliances)
        appliances = result or {}
        for appliance_id, appliance in appliances.items():
            self._metrics.setdefault(appliance_id, ApplianceMetrics()).online = (
                appliance.get("online")
            )
        return appliances

    async def _list_appliances(
        self, cloud: Any
    ) -> dict[int, dict[str, Any]] | None:
        """List Midea Air appliances while surfacing session error codes.

        midea-local's public ``list_appliances`` does not enable
        ``raise_for_error``, so 3106/3144 become an indistinguishable ``None``.
        The integration is pinned to midea-local 12.2.0 and already relies on
        these private methods for transparent-send.
        """
        if not hasattr(cloud, "_make_general_data") or not hasattr(
            cloud, "_api_request"
        ):
            # Test doubles and future compatible adapters may only expose the
            # public API. Production MideaAirCloud follows the branch below.
            return await cloud.list_appliances(None)

        response = await cloud._api_request(
            endpoint=_APPLIANCE_LIST_ENDPOINT,
            data=cloud._make_general_data(),
            raise_for_error=True,
        )
        if response is None:
            return None

        appliances: dict[int, dict[str, Any]] = {}
        for appliance in response["list"]:
            try:
                model_number = int(appliance.get("modelNumber", 0))
            except ValueError:
                model_number = 0
            device_info: dict[str, Any] = {
                "name": appliance.get("name"),
                "type": int(appliance.get("type"), 16),
                "sn": appliance.get("sn"),
                "sn8": "",
                "model_number": model_number,
                "manufacturer_code": appliance.get("enterpriseCode", "0000"),
                "model": "",
                "online": appliance.get("onlineStatus") == "1",
            }
            serial_number = device_info.get("sn")
            device_info["sn8"] = (
                serial_number[9:17]
                if serial_number and len(serial_number) > _SN8_MIN_SERIAL_LENGTH
                else ""
            )
            device_info["model"] = device_info["sn8"]
            appliances[int(appliance["id"])] = device_info
        return appliances

    async def query(self, appliance_id: int) -> StateResponse:
        """Read current state while serializing against commands for the device."""
        device_lock = self._device_locks.setdefault(appliance_id, asyncio.Lock())
        async with device_lock:
            return await self._query(appliance_id)

    async def _query(
        self, appliance_id: int, *, max_attempts: int | None = None
    ) -> StateResponse:
        """Read current state; the caller must hold the per-device lock."""
        metrics = self._metrics.setdefault(appliance_id, ApplianceMetrics())
        metrics.status_requests += 1
        started = self._monotonic()

        async def operation(cloud: Any) -> StateResponse:
            reply = await transparent_send(
                cloud, appliance_id, GetStateCommand().tobytes(), self._app_key
            )
            if reply is None:
                raise _TransientRequestError("Cloud returned no appliance reply")
            return self._decode_state_response(reply, "appliance")

        try:
            state = await self._execute(
                operation,
                max_attempts=(
                    max_attempts
                    if max_attempts is not None
                    else self._policy.status_attempts
                ),
                retry_protocol=True,
                transient_backoff=self._status_backoff,
                metrics=metrics,
                operation_kind="status",
            )
            metrics.online = True
            return state
        finally:
            elapsed = max(0.0, self._monotonic() - started)
            metrics.last_status_seconds = elapsed
            metrics.total_status_seconds += elapsed

    async def set_state(
        self, appliance_id: int, *, beep: bool = False, **changes: Any
    ) -> StateResponse:
        """Apply changes on top of current state, then return the acknowledgement."""
        unknown = set(changes) - set(_CARRIED_STATE)
        if unknown:
            raise ValueError(f"unsupported field(s): {sorted(unknown)}")

        device_lock = self._device_locks.setdefault(appliance_id, asyncio.Lock())
        async with device_lock:
            metrics = self._metrics.setdefault(appliance_id, ApplianceMetrics())
            metrics.command_requests += 1
            current = await self._query(appliance_id)
            command = SetStateCommand()
            command.beep_on = beep
            for field in _CARRIED_STATE:
                setattr(command, field, getattr(current, field))
            for field, value in changes.items():
                setattr(command, field, value)

            async def operation(cloud: Any) -> StateResponse:
                try:
                    reply = await transparent_send(
                        cloud, appliance_id, command.tobytes(), self._app_key
                    )
                except TransparentSendDecodeError as err:
                    metrics.protocol_errors += 1
                    raise CloudCommandUncertainError(
                        "Cloud command reply could not be decoded"
                    ) from err
                if reply is None:
                    raise _TransientRequestError("Cloud did not acknowledge command")
                try:
                    return self._decode_state_response(reply, "command")
                except CloudProtocolError as err:
                    metrics.protocol_errors += 1
                    raise CloudCommandUncertainError(
                        "Cloud command reply could not be decoded"
                    ) from err

            try:
                return await self._execute(
                    operation,
                    retry_transient=False,
                    metrics=metrics,
                    operation_kind="command",
                )
            except CloudCommandUncertainError as err:
                return await self._verify_uncertain_command(
                    appliance_id, changes, err
                )

    async def _execute(
        self,
        operation: Callable[[Any], Awaitable[_T]],
        *,
        max_attempts: int | None = None,
        retry_transient: bool = True,
        retry_protocol: bool = False,
        transient_backoff: Callable[[int], Awaitable[None]] | None = None,
        metrics: ApplianceMetrics | None = None,
        operation_kind: str | None = None,
    ) -> _T:
        last_error: Exception | None = None
        session_refreshes = 0
        protocol_retries = 0
        attempts = (
            max_attempts if max_attempts is not None else self._policy.request_attempts
        )

        for attempt in range(attempts):
            if metrics is not None:
                if operation_kind == "status":
                    metrics.status_attempts += 1
                elif operation_kind == "command":
                    metrics.command_attempts += 1
            await self.connect()
            cloud = self._cloud
            generation = self._generation
            try:
                result = await self._limited_call(lambda cloud=cloud: operation(cloud))
                if result is None:
                    raise _TransientRequestError("Cloud returned an empty response")
                return result
            except MideaCloudError as err:
                if err.code in _SESSION_ERROR_CODES:
                    if session_refreshes >= 1:
                        raise CloudAuthenticationError(
                            f"Cloud session remained invalid after refresh (code {err.code})"
                        ) from err
                    await self._refresh_if_current(generation)
                    session_refreshes += 1
                    continue
                if err.code not in _REQUEST_TRANSIENT_ERROR_CODES:
                    raise CloudError(
                        f"Cloud request was rejected with code {err.code}"
                    ) from err
                last_error = err
                if metrics is not None and err.code == 3176:
                    metrics.missing_replies += 1
            except (_TransientRequestError, aiohttp.ClientError, TimeoutError, OSError) as err:
                last_error = err
                if metrics is not None and isinstance(err, _TransientRequestError):
                    metrics.missing_replies += 1
            except (CloudProtocolError, TransparentSendDecodeError) as err:
                if metrics is not None:
                    metrics.protocol_errors += 1
                protocol_error = (
                    err
                    if isinstance(err, CloudProtocolError)
                    else CloudProtocolError("Could not decode appliance response")
                )
                if not retry_protocol or protocol_retries >= 1:
                    raise protocol_error from err
                protocol_retries += 1
                last_error = protocol_error

            if not retry_transient:
                raise CloudCommandUncertainError(
                    "Cloud command acknowledgement was not received"
                ) from last_error

            if attempt < attempts - 1:
                await (transient_backoff or self._backoff)(attempt)

        raise CloudUnavailableError(
            f"Cloud request failed after {attempts} attempts"
        ) from last_error

    async def _verify_uncertain_command(
        self,
        appliance_id: int,
        changes: dict[str, Any],
        command_error: CloudCommandUncertainError,
    ) -> StateResponse:
        """Verify an uncertain command without ever sending it a second time."""
        last_query_error: CloudError | None = None
        for attempt in range(self._policy.command_verification_attempts):
            await self._backoff(attempt)
            try:
                state = await self._query(appliance_id, max_attempts=1)
            except CloudError as err:
                last_query_error = err
                continue
            if self._state_matches_changes(state, changes):
                return state
            last_query_error = None

        error = CloudCommandUncertainError(
            "Cloud command acknowledgement was missing and the requested state "
            "could not be verified"
        )
        if last_query_error is not None:
            raise error from last_query_error
        raise error from command_error

    @staticmethod
    def _state_matches_changes(state: StateResponse, changes: dict[str, Any]) -> bool:
        """Return whether the explicitly requested fields match current state."""
        for field, expected in changes.items():
            actual = getattr(state, field, None)
            if (
                isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and isinstance(actual, (int, float))
                and not isinstance(actual, bool)
            ):
                if not math.isclose(float(actual), float(expected), abs_tol=0.1):
                    return False
            elif actual != expected:
                return False
        return True

    def appliance_diagnostics(self, appliance_id: int) -> dict[str, Any]:
        """Return non-sensitive counters for Home Assistant diagnostics."""
        metrics = self._metrics.get(appliance_id, ApplianceMetrics())
        average = (
            metrics.total_status_seconds / metrics.status_requests
            if metrics.status_requests
            else None
        )
        return {
            "status_requests": metrics.status_requests,
            "status_attempts": metrics.status_attempts,
            "command_requests": metrics.command_requests,
            "command_attempts": metrics.command_attempts,
            "missing_replies": metrics.missing_replies,
            "protocol_errors": metrics.protocol_errors,
            "online": metrics.online,
            "average_status_seconds": average,
            "last_status_seconds": metrics.last_status_seconds,
        }

    @staticmethod
    def _decode_state_response(reply: bytes, context: str) -> StateResponse:
        """Decode an msmart state response into a controlled cloud error."""
        try:
            response = Response.construct(reply)
        except (
            IndexError,
            InvalidFrameException,
            InvalidResponseException,
            ValueError,
        ) as err:
            raise CloudProtocolError(f"Could not decode {context} response") from err
        if not isinstance(response, StateResponse):
            raise CloudProtocolError(
                f"Unexpected {context} response type {type(response).__name__}"
            )
        return response

    @staticmethod
    def _validate_cloud_private_api(cloud: Any) -> None:
        """Fail clearly if pinned midea-local internals are no longer available."""
        required = ("_access_token", "_api_request", "_make_general_data")
        missing = [name for name in required if not hasattr(cloud, name)]
        if missing:
            raise CloudProtocolError(
                "Installed midea-local is incompatible; missing private API: "
                + ", ".join(missing)
            )

    async def _refresh_if_current(self, failed_generation: int) -> None:
        """Refresh once when concurrent requests observe the same expired session."""
        async with self._auth_lock:
            if self._cloud is not None and self._generation != failed_generation:
                return
            self._cloud = None
            await self._connect_locked()

    async def _limited_call(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        async with self._request_semaphore:
            async with self._rate_lock:
                delay = self._next_request_at - self._monotonic()
                if delay > 0:
                    await self._sleep(delay)
                self._next_request_at = self._monotonic() + self._min_request_interval
            return await operation()

    async def _backoff(self, attempt: int) -> None:
        base = min(
            self._policy.maximum_delay,
            self._policy.base_delay * (2**attempt),
        )
        jitter = base * self._policy.jitter * self._random()
        await self._sleep(base + jitter)

    async def _status_backoff(self, attempt: int) -> None:
        """Pause long enough for other devices to use the account queue."""
        delay = self._policy.status_retry_delay * (2**attempt)
        jitter = self._policy.status_retry_jitter * self._random()
        await self._sleep(delay + jitter)
