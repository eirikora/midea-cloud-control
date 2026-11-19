"""Polling coordinator for Midea cloud-controlled appliances."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from msmart.device.AC.command import StateResponse

from .client import CloudError, MideaCloudClient
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    POLL_SPREAD_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class MideaCloudCoordinator(DataUpdateCoordinator[StateResponse]):
    """Poll appliance state through the vendor cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MideaCloudClient,
        appliance_id: int,
        name: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL_SECONDS,
    ) -> None:
        base_interval = max(
            MIN_SCAN_INTERVAL_SECONDS,
            min(MAX_SCAN_INTERVAL_SECONDS, scan_interval),
        )
        spread = min(POLL_SPREAD_SECONDS, MAX_SCAN_INTERVAL_SECONDS - base_interval)
        offset = appliance_id % (spread + 1) if spread else 0
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=timedelta(seconds=base_interval + offset),
        )
        self.client = client
        self.appliance_id = appliance_id
        self.appliance_name = name

    async def _async_update_data(self) -> StateResponse:
        try:
            return await self.client.query(self.appliance_id)
        except CloudError as err:
            raise UpdateFailed(f"Cloud query failed: {err}") from err

    async def async_set_state(self, **changes: object) -> None:
        """Push a state change, then refresh so the UI reflects the device."""
        try:
            await self.client.set_state(self.appliance_id, **changes)
        except CloudError as err:
            raise UpdateFailed(f"Cloud command failed: {err}") from err
        await self.async_request_refresh()
