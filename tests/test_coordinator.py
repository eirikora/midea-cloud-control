"""Coordinator availability and recovery tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.midea_cloud_control.client import CloudUnavailableError
from custom_components.midea_cloud_control.coordinator import MideaCloudCoordinator


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
