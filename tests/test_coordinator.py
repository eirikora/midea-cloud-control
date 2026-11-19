"""Coordinator availability and recovery tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.midea_cloud_control.client import (
    CloudInvalidAuthError,
    CloudUnavailableError,
)
from custom_components.midea_cloud_control.coordinator import (
    MideaCloudCoordinator,
    MideaCloudPollScheduler,
)


class FakeConfigEntry:
    """Minimum config-entry surface required by DataUpdateCoordinator."""

    def async_on_unload(self, _callback) -> None:
        return None


@pytest.mark.asyncio
async def test_network_failure_marks_update_failed_then_recovers(tmp_path) -> None:
    recovered_state = object()
    client = AsyncMock()
    client.query.side_effect = [
        CloudUnavailableError("offline"),
        recovered_state,
    ]
    hass = HomeAssistant(str(tmp_path))
    coordinator = MideaCloudCoordinator(
        hass, FakeConfigEntry(), client, 1, "Bedroom"  # type: ignore[arg-type]
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert await coordinator._async_update_data() is recovered_state


@pytest.mark.asyncio
async def test_invalid_runtime_credentials_start_reauthentication(tmp_path) -> None:
    client = AsyncMock()
    client.query.side_effect = CloudInvalidAuthError("rejected")
    coordinator = MideaCloudCoordinator(
        HomeAssistant(str(tmp_path)),
        FakeConfigEntry(),  # type: ignore[arg-type]
        client,
        1,
        "Bedroom",
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_stale_state_survives_two_transient_poll_failures(tmp_path) -> None:
    stale_state = object()
    client = AsyncMock()
    client.query.side_effect = [
        CloudUnavailableError("temporary"),
        CloudUnavailableError("temporary"),
        CloudUnavailableError("persistent"),
    ]
    coordinator = MideaCloudCoordinator(
        HomeAssistant(str(tmp_path)),
        FakeConfigEntry(),  # type: ignore[arg-type]
        client,
        1,
        "Bedroom",
    )
    coordinator.data = stale_state

    assert await coordinator._async_update_data() is stale_state
    assert await coordinator._async_update_data() is stale_state
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.consecutive_failures == 3


@pytest.mark.asyncio
async def test_command_response_updates_coordinator_without_refresh(tmp_path) -> None:
    new_state = object()
    client = AsyncMock()
    client.set_state.return_value = new_state
    coordinator = MideaCloudCoordinator(
        HomeAssistant(str(tmp_path)),
        FakeConfigEntry(),  # type: ignore[arg-type]
        client,
        1,
        "Bedroom",
    )
    coordinator.async_request_refresh = AsyncMock()

    await coordinator.async_set_state(power_on=True)

    assert coordinator.data is new_state
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_coordinators_use_one_common_poll_interval(tmp_path) -> None:
    hass = HomeAssistant(str(tmp_path))
    first = MideaCloudCoordinator(
        hass, FakeConfigEntry(), AsyncMock(), 1, "One"  # type: ignore[arg-type]
    )
    second = MideaCloudCoordinator(
        hass, FakeConfigEntry(), AsyncMock(), 999, "Two"  # type: ignore[arg-type]
    )

    assert first.poll_interval == second.poll_interval


@pytest.mark.asyncio
async def test_poll_scheduler_uses_stable_round_robin_phases(tmp_path) -> None:
    hass = HomeAssistant(str(tmp_path))
    client = AsyncMock()
    client.query.side_effect = lambda appliance_id: appliance_id
    coordinators = [
        MideaCloudCoordinator(
            hass,
            FakeConfigEntry(),  # type: ignore[arg-type]
            client,
            appliance_id,
            str(appliance_id),
            scan_interval=90,
        )
        for appliance_id in (3, 1, 2)
    ]
    scheduler = MideaCloudPollScheduler(hass, coordinators)

    assert scheduler.spacing_seconds == 30
    for _index in range(4):
        task = scheduler._dispatch_next()
        assert task is not None
        await task

    assert [call.args[0] for call in client.query.await_args_list] == [1, 2, 3, 1]
