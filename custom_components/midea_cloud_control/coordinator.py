"""Polling coordinator for Midea cloud-controlled appliances."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from msmart.device.AC.command import StateResponse

from .client import (
    CloudError,
    CloudInvalidAuthError,
    CloudUnavailableError,
    MideaCloudClient,
)
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    STALE_GRACE_POLLS,
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
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            # Polling is driven by MideaCloudPollScheduler so every appliance
            # keeps a fixed account-wide phase instead of forming a burst.
            update_interval=None,
        )
        self.poll_interval = timedelta(seconds=base_interval)
        self.client = client
        self.appliance_id = appliance_id
        self.appliance_name = name
        self.consecutive_failures = 0
        self.last_success_at = None

    async def _async_update_data(self) -> StateResponse:
        try:
            state = await self.client.query(self.appliance_id)
        except CloudInvalidAuthError as err:
            raise ConfigEntryAuthFailed("Cloud credentials were rejected") from err
        except CloudUnavailableError as err:
            self.consecutive_failures += 1
            if self.data is not None and self.consecutive_failures < STALE_GRACE_POLLS:
                _LOGGER.debug(
                    "Keeping last cloud state after transient failure %s/%s: %s",
                    self.consecutive_failures,
                    STALE_GRACE_POLLS - 1,
                    err,
                )
                return self.data
            raise UpdateFailed(f"Cloud query failed: {err}") from err
        except CloudError as err:
            raise UpdateFailed(f"Cloud query failed: {err}") from err
        self.consecutive_failures = 0
        self.last_success_at = dt_util.utcnow()
        return state

    async def async_set_state(self, **changes: object) -> None:
        """Push a state change, then refresh so the UI reflects the device."""
        try:
            state = await self.client.set_state(self.appliance_id, **changes)
        except CloudError as err:
            raise HomeAssistantError(f"Cloud command failed: {err}") from err
        self.consecutive_failures = 0
        self.last_success_at = dt_util.utcnow()
        self.async_set_updated_data(state)


class MideaCloudPollScheduler:
    """Poll one account entry at evenly spaced, stable round-robin phases."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinators: list[MideaCloudCoordinator],
    ) -> None:
        self._hass = hass
        self._coordinators = sorted(
            coordinators, key=lambda coordinator: coordinator.appliance_id
        )
        interval = (
            self._coordinators[0].poll_interval.total_seconds()
            if self._coordinators
            else DEFAULT_SCAN_INTERVAL_SECONDS
        )
        self.spacing_seconds = interval / max(1, len(self._coordinators))
        self._initial_delay = self.spacing_seconds
        if len(self._coordinators) == 1:
            # Migrated single-appliance entries share a session but not a
            # scheduler. A deterministic phase prevents them all firing at once.
            phase_range = max(1, int(interval) - 1)
            self._initial_delay = 1 + (
                self._coordinators[0].appliance_id % phase_range
            )
        self._next_index = 0
        self._runner: asyncio.Task[None] | None = None
        self._in_flight: dict[int, asyncio.Task[None]] = {}

    def start(self) -> None:
        """Start background polling after all initial refreshes have completed."""
        if self._runner is not None or not self._coordinators:
            return
        self._runner = self._hass.async_create_background_task(
            self._async_run(), f"{DOMAIN} round-robin polling"
        )

    async def async_stop(self) -> None:
        """Stop scheduling and cancel refreshes owned by this scheduler."""
        tasks = [*self._in_flight.values()]
        if self._runner is not None:
            tasks.append(self._runner)
            self._runner = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._in_flight.clear()

    async def _async_run(self) -> None:
        delay = self._initial_delay
        while True:
            await asyncio.sleep(delay)
            self._dispatch_next()
            delay = self.spacing_seconds

    def _dispatch_next(self) -> asyncio.Task[None] | None:
        """Dispatch the next phase unless that appliance is still refreshing."""
        coordinator = self._coordinators[self._next_index]
        self._next_index = (self._next_index + 1) % len(self._coordinators)
        key = coordinator.appliance_id
        current = self._in_flight.get(key)
        if current is not None and not current.done():
            _LOGGER.debug(
                "Skipping overlapping cloud poll for appliance %s", key
            )
            return None
        if getattr(coordinator.config_entry, "pref_disable_polling", False):
            return None

        task = self._hass.async_create_background_task(
            coordinator.async_refresh(), f"{DOMAIN} poll {key}"
        )
        self._in_flight[key] = task
        task.add_done_callback(
            lambda completed, appliance_id=key: self._discard_task(
                appliance_id, completed
            )
        )
        return task

    def _discard_task(
        self, appliance_id: int, completed: asyncio.Task[None]
    ) -> None:
        if self._in_flight.get(appliance_id) is completed:
            self._in_flight.pop(appliance_id, None)
