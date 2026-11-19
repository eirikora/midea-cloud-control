"""Options-flow tests for safe use of the shared cloud session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from midealocal.exceptions import MideaCloudError

from custom_components.midea_cloud_control.client import MideaCloudClient, RetryPolicy
from custom_components.midea_cloud_control.config_flow import MideaCloudOptionsFlow
from custom_components.midea_cloud_control.const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_CLOUD,
    CONF_SCAN_INTERVAL,
    DATA_SESSION_REGISTRY,
    DOMAIN,
)
from custom_components.midea_cloud_control.session import CloudSessionRegistry


def appliance_response() -> dict[str, list[dict[str, str]]]:
    """Return the raw Midea Air list payload expected by the private adapter."""
    return {
        "list": [
            {
                "id": "123",
                "name": "Bedroom",
                "type": "0xac",
                "sn": "123456789ABCDEFGH1",
                "modelNumber": "1",
                "enterpriseCode": "0000",
                "onlineStatus": "1",
            }
        ]
    }


class PrivateCloud:
    """Script the private MideaAirCloud methods used by appliance listing."""

    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.login_calls = 0
        self.request_calls = 0
        self.raise_for_error_values: list[bool] = []

    async def login(self) -> bool:
        self.login_calls += 1
        return True

    def _make_general_data(self) -> dict[str, str]:
        return {"sessionId": "session"}

    async def _api_request(
        self, *, endpoint: str, data: dict[str, Any], raise_for_error: bool
    ) -> dict[str, Any]:
        assert endpoint == "/v1/appliance/user/list/get"
        assert data["sessionId"] == "session"
        self.request_calls += 1
        self.raise_for_error_values.append(raise_for_error)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class SharedClientFactory:
    """Create real resilient clients around scripted private cloud objects."""

    def __init__(self, clouds: list[PrivateCloud]) -> None:
        self.clouds = clouds
        self.clients: list[MideaCloudClient] = []

    def __call__(
        self,
        account: str,
        password: str,
        cloud_name: str,
        **kwargs: Any,
    ) -> MideaCloudClient:
        async def no_sleep(_delay: float) -> None:
            return None

        def cloud_factory(*_args: object) -> PrivateCloud:
            return self.clouds.pop(0)

        client = MideaCloudClient(
            account,
            password,
            cloud_name,
            **kwargs,
            cloud_factory=cloud_factory,
            retry_policy=RetryPolicy(base_delay=0, jitter=0),
            sleep=no_sleep,
        )
        self.clients.append(client)
        return client


def make_entry() -> SimpleNamespace:
    """Create the config-entry surface used by the options flow."""
    return SimpleNamespace(
        entry_id="disabled-entry",
        data={
            CONF_USERNAME: "user@example.invalid",
            CONF_PASSWORD: "not-real",
            CONF_CLOUD: "NetHome Plus",
            CONF_APPLIANCES: [
                {CONF_APPLIANCE_ID: 123, CONF_APPLIANCE_NAME: "Bedroom"}
            ],
            CONF_SCAN_INTERVAL: 90,
        },
        options={},
    )


async def open_options(
    registry: CloudSessionRegistry, entry: SimpleNamespace
) -> dict[str, Any]:
    """Open Configure without loading a runtime for the configured entry."""
    hass = SimpleNamespace(data={DOMAIN: {DATA_SESSION_REGISTRY: registry}})
    flow = MideaCloudOptionsFlow(entry)  # type: ignore[arg-type]
    flow.hass = hass  # type: ignore[assignment]
    return await flow.async_step_init()


@pytest.mark.asyncio
async def test_configure_reuses_valid_session_for_disabled_entry() -> None:
    cloud = PrivateCloud(appliance_response())
    factory = SharedClientFactory([cloud])
    registry = CloudSessionRegistry(
        object(), client_factory=factory, min_request_interval=0  # type: ignore[arg-type]
    )
    await registry.acquire("user@example.invalid", "not-real", "NetHome Plus")

    result = await open_options(registry, make_entry())

    assert result["type"] is FlowResultType.FORM
    assert cloud.login_calls == 1
    assert cloud.request_calls == 1
    assert cloud.raise_for_error_values == [True]
    assert registry.active_sessions == 1
    assert await registry.references("user@example.invalid", "NetHome Plus") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [3106, 3144])
async def test_configure_renews_expired_shared_session(error_code: int) -> None:
    expired = PrivateCloud(MideaCloudError(error_code, "expired"))
    renewed = PrivateCloud(appliance_response())
    factory = SharedClientFactory([expired, renewed])
    registry = CloudSessionRegistry(
        object(), client_factory=factory, min_request_interval=0  # type: ignore[arg-type]
    )
    client = await registry.acquire(
        "user@example.invalid", "not-real", "NetHome Plus"
    )

    result = await open_options(registry, make_entry())

    assert result["type"] is FlowResultType.FORM
    assert expired.login_calls == 1
    assert renewed.login_calls == 1
    assert client.generation == 2
    assert expired.raise_for_error_values == [True]
    assert renewed.raise_for_error_values == [True]
    assert await registry.references("user@example.invalid", "NetHome Plus") == 1
