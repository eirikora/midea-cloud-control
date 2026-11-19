"""Tests for shared authentication and resilient cloud requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from midealocal.exceptions import MideaCloudError

from custom_components.midea_cloud_control import client as client_module
from custom_components.midea_cloud_control.client import (
    CloudAuthenticationError,
    CloudCommandUncertainError,
    CloudCooldownError,
    CloudInvalidAuthError,
    CloudUnavailableError,
    MideaCloudClient,
    RetryPolicy,
)


class FakeCloud:
    """Small scripted replacement for midea-local's MideaAirCloud."""

    def __init__(
        self,
        *,
        login: bool | Exception = True,
        responses: list[object] | None = None,
        delay: float = 0,
    ) -> None:
        self.login_result = login
        self.responses = responses or [{}]
        self.delay = delay
        self.login_calls = 0
        self.request_calls = 0
        self.active = 0
        self.max_active = 0

    async def login(self) -> bool:
        self.login_calls += 1
        await asyncio.sleep(0)
        if isinstance(self.login_result, Exception):
            raise self.login_result
        return self.login_result

    async def list_appliances(self, _home_id: object) -> object:
        self.request_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            self.active -= 1


def make_client(
    factory: Callable[..., FakeCloud],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], object] | None = None,
    max_concurrent_requests: int = 2,
) -> MideaCloudClient:
    async def no_sleep(_delay: float) -> None:
        return None

    return MideaCloudClient(
        "user@example.invalid",
        "not-a-real-password",
        "NetHome Plus",
        session=object(),  # type: ignore[arg-type]
        cloud_factory=factory,
        retry_policy=policy or RetryPolicy(base_delay=0, jitter=0),
        sleep=sleep or no_sleep,  # type: ignore[arg-type]
        min_request_interval=0,
        max_concurrent_requests=max_concurrent_requests,
    )


@pytest.mark.asyncio
async def test_parallel_connect_performs_one_login() -> None:
    cloud = FakeCloud()
    factory_calls = 0

    def factory(*_args: object) -> FakeCloud:
        nonlocal factory_calls
        factory_calls += 1
        return cloud

    client = make_client(factory)
    await asyncio.gather(*(client.connect() for _ in range(15)))

    assert factory_calls == 1
    assert cloud.login_calls == 1
    assert client.generation == 1


@pytest.mark.asyncio
async def test_3106_replaces_expired_cloud_object_once() -> None:
    clouds = [
        FakeCloud(responses=[MideaCloudError(3106, "invalidSession")]),
        FakeCloud(responses=[{1: {"name": "Bedroom"}}]),
    ]

    def factory(*_args: object) -> FakeCloud:
        return clouds.pop(0)

    client = make_client(factory)
    result = await client.list_appliances()

    assert result == {1: {"name": "Bedroom"}}
    assert client.generation == 2
    assert not clouds


@pytest.mark.asyncio
async def test_3144_login_has_bounded_backoff_and_fresh_objects() -> None:
    created: list[FakeCloud] = []
    sleeps: list[float] = []

    def factory(*_args: object) -> FakeCloud:
        cloud = FakeCloud(
            login=MideaCloudError(3144, "loginId is empty")
            if len(created) < 2
            else True
        )
        created.append(cloud)
        return cloud

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = make_client(
        factory,
        policy=RetryPolicy(login_attempts=3, base_delay=1, jitter=0),
        sleep=record_sleep,
    )
    await client.connect()

    assert len(created) == 3
    assert sleeps == [1, 2]
    assert client.generation == 1


@pytest.mark.asyncio
async def test_repeated_3144_enters_cooldown_without_login_loop() -> None:
    calls = 0

    def factory(*_args: object) -> FakeCloud:
        nonlocal calls
        calls += 1
        return FakeCloud(login=MideaCloudError(3144, "loginId is empty"))

    client = make_client(
        factory,
        policy=RetryPolicy(login_attempts=2, base_delay=0, jitter=0, cooldown=60),
    )

    with pytest.raises(CloudAuthenticationError):
        await client.connect()
    with pytest.raises(CloudCooldownError):
        await client.connect()
    assert calls == 2


@pytest.mark.asyncio
async def test_invalid_credentials_are_not_retried() -> None:
    cloud = FakeCloud(login=MideaCloudError(3101, "invalid credentials"))
    client = make_client(lambda *_args: cloud)

    with pytest.raises(CloudInvalidAuthError):
        await client.connect()
    assert cloud.login_calls == 1


@pytest.mark.asyncio
async def test_9999_is_retried_then_surfaced_without_reauthentication() -> None:
    cloud = FakeCloud(
        responses=[
            MideaCloudError(9999, "system error"),
            MideaCloudError(9999, "system error"),
            MideaCloudError(9999, "system error"),
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = make_client(
        lambda *_args: cloud,
        policy=RetryPolicy(request_attempts=3, base_delay=1, jitter=0),
        sleep=record_sleep,
    )

    with pytest.raises(CloudUnavailableError):
        await client.list_appliances()
    assert cloud.request_calls == 3
    assert cloud.login_calls == 1
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_3176_none_during_polling_is_retried_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """midea-local logs 3176 and returns None even with raise_for_error."""
    cloud = FakeCloud()
    sleeps: list[float] = []
    send_calls = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = make_client(
        lambda *_args: cloud,
        policy=RetryPolicy(status_attempts=3, base_delay=1, jitter=0),
        sleep=record_sleep,
    )
    await client.connect()

    class FakeState:
        pass

    class FakeResponse:
        @staticmethod
        def construct(_reply: bytes) -> FakeState:
            return FakeState()

    async def fake_send(*_args: object) -> bytes | None:
        nonlocal send_calls
        send_calls += 1
        return None if send_calls == 1 else b"reply"

    monkeypatch.setattr(client_module, "StateResponse", FakeState)
    monkeypatch.setattr(client_module, "Response", FakeResponse)
    monkeypatch.setattr(client_module, "transparent_send", fake_send)

    assert isinstance(await client.query(123), FakeState)
    assert send_calls == 2
    assert sleeps == [1]
    assert cloud.login_calls == 1
    assert client.generation == 1


@pytest.mark.asyncio
async def test_persistent_3176_polling_is_reported_without_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = FakeCloud()
    sleeps: list[float] = []
    send_calls = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = make_client(
        lambda *_args: cloud,
        policy=RetryPolicy(status_attempts=3, base_delay=1, jitter=0),
        sleep=record_sleep,
    )

    async def fake_send(*_args: object) -> bytes:
        nonlocal send_calls
        send_calls += 1
        raise MideaCloudError(3176, "The asyn reply does not exist")

    monkeypatch.setattr(client_module, "transparent_send", fake_send)

    with pytest.raises(CloudUnavailableError):
        await client.query(123)
    assert send_calls == 3
    assert sleeps == [1, 2]
    assert cloud.login_calls == 1
    assert client.generation == 1


@pytest.mark.asyncio
async def test_network_recovers_on_next_poll() -> None:
    cloud = FakeCloud(
        responses=[
            aiohttp.ClientConnectionError("offline"),
            aiohttp.ClientConnectionError("offline"),
            aiohttp.ClientConnectionError("offline"),
            {7: {"name": "Recovered"}},
        ]
    )
    client = make_client(lambda *_args: cloud)

    with pytest.raises(CloudUnavailableError):
        await client.list_appliances()
    assert await client.list_appliances() == {7: {"name": "Recovered"}}
    assert cloud.login_calls == 1


@pytest.mark.asyncio
async def test_fifteen_requests_obey_shared_concurrency_limit() -> None:
    cloud = FakeCloud(responses=[{1: {}}], delay=0.01)
    client = make_client(lambda *_args: cloud, max_concurrent_requests=2)

    await asyncio.gather(*(client.list_appliances() for _ in range(15)))

    assert cloud.max_active == 2
    assert cloud.login_calls == 1


@pytest.mark.asyncio
async def test_temperature_change_targets_only_requested_appliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = FakeCloud()
    client = make_client(lambda *_args: cloud)
    await client.connect()

    current = SimpleNamespace(
        **{field: False for field in client_module._CARRIED_STATE}
    )
    current.target_temperature = 21.0
    client.query = AsyncMock(return_value=current)  # type: ignore[method-assign]

    sent: list[tuple[int, float]] = []

    class FakeCommand:
        target_temperature = 0.0

        def tobytes(self) -> bytes:
            return b"command"

    class FakeState:
        pass

    class FakeResponse:
        @staticmethod
        def construct(_reply: bytes) -> FakeState:
            return FakeState()

    async def fake_send(
        _cloud: object, appliance_id: int, _frame: bytes, _app_key: str
    ) -> bytes:
        sent.append((appliance_id, command.target_temperature))
        return b"reply"

    command = FakeCommand()
    monkeypatch.setattr(client_module, "SetStateCommand", lambda: command)
    monkeypatch.setattr(client_module, "StateResponse", FakeState)
    monkeypatch.setattr(client_module, "Response", FakeResponse)
    monkeypatch.setattr(client_module, "transparent_send", fake_send)

    await client.set_state(222, target_temperature=23.5)

    assert sent == [(222, 23.5)]


@pytest.mark.asyncio
async def test_3176_command_is_sent_once_then_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = FakeCloud()
    sleeps: list[float] = []
    client = make_client(
        lambda *_args: cloud,
        policy=RetryPolicy(
            command_verification_attempts=2, base_delay=1, jitter=0
        ),
        sleep=lambda delay: _record_delay(sleeps, delay),
    )
    await client.connect()

    current = SimpleNamespace(
        **{field: False for field in client_module._CARRIED_STATE}
    )
    verified = SimpleNamespace(
        **{field: False for field in client_module._CARRIED_STATE}
    )
    verified.power_on = True
    client.query = AsyncMock(side_effect=[current, verified])  # type: ignore[method-assign]

    send_calls = 0

    class FakeCommand:
        def tobytes(self) -> bytes:
            return b"command"

    async def fake_send(*_args: object) -> None:
        nonlocal send_calls
        send_calls += 1
        return None

    monkeypatch.setattr(client_module, "SetStateCommand", FakeCommand)
    monkeypatch.setattr(client_module, "transparent_send", fake_send)

    assert await client.set_state(222, power_on=True) is verified
    assert send_calls == 1
    assert client.query.await_count == 2
    assert sleeps == [1]
    assert cloud.login_calls == 1


@pytest.mark.asyncio
async def test_3176_command_is_never_replayed_when_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = FakeCloud()
    sleeps: list[float] = []
    client = make_client(
        lambda *_args: cloud,
        policy=RetryPolicy(
            command_verification_attempts=2, base_delay=1, jitter=0
        ),
        sleep=lambda delay: _record_delay(sleeps, delay),
    )

    current = SimpleNamespace(
        **{field: False for field in client_module._CARRIED_STATE}
    )
    mismatch = SimpleNamespace(
        **{field: False for field in client_module._CARRIED_STATE}
    )
    client.query = AsyncMock(  # type: ignore[method-assign]
        side_effect=[current, mismatch, mismatch]
    )

    send_calls = 0

    class FakeCommand:
        def tobytes(self) -> bytes:
            return b"command"

    async def fake_send(*_args: object) -> None:
        nonlocal send_calls
        send_calls += 1
        return None

    monkeypatch.setattr(client_module, "SetStateCommand", FakeCommand)
    monkeypatch.setattr(client_module, "transparent_send", fake_send)

    with pytest.raises(CloudCommandUncertainError):
        await client.set_state(222, power_on=True)
    assert send_calls == 1
    assert client.query.await_count == 3
    assert sleeps == [1, 2]


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)
