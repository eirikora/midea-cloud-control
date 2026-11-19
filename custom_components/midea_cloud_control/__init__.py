"""Midea Cloud Control integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .client import CloudError, CloudInvalidAuthError, MideaCloudClient
from .const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_CLOUD,
    CONF_SCAN_INTERVAL,
    DATA_SESSION_REGISTRY,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import MideaCloudCoordinator
from .session import CloudSessionRegistry, get_session_registry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MideaCloudRuntimeData:
    """Runtime resources owned by one config entry."""

    client: MideaCloudClient
    coordinators: list[MideaCloudCoordinator]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Convert a version-1 single-appliance entry without changing entity IDs."""
    if entry.version != 1:
        return True

    data = dict(entry.data)
    appliance = {
        CONF_APPLIANCE_ID: int(data.pop(CONF_APPLIANCE_ID)),
        CONF_APPLIANCE_NAME: data.pop(CONF_APPLIANCE_NAME),
    }
    data[CONF_APPLIANCES] = [appliance]
    data.setdefault(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    _LOGGER.info("Migrated Midea Cloud Control entry to account-based schema")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one account entry containing one or more appliances."""
    registry = get_session_registry(hass)
    account = entry.data[CONF_USERNAME]
    cloud_name = entry.data[CONF_CLOUD]

    try:
        client = await registry.acquire(
            account, entry.data[CONF_PASSWORD], cloud_name
        )
    except CloudInvalidAuthError as err:
        raise ConfigEntryAuthFailed("Cloud credentials were rejected") from err
    except (CloudError, ValueError) as err:
        raise ConfigEntryNotReady(
            f"Could not establish shared {cloud_name} session: {err}"
        ) from err

    appliances = entry.options.get(CONF_APPLIANCES, entry.data[CONF_APPLIANCES])
    scan_interval = int(
        entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
        )
    )
    coordinators = [
        MideaCloudCoordinator(
            hass,
            entry,
            client,
            int(appliance[CONF_APPLIANCE_ID]),
            appliance[CONF_APPLIANCE_NAME],
            scan_interval=scan_interval,
        )
        for appliance in appliances
    ]

    try:
        # A single offline unit must not prevent the other 14 entities loading.
        # The request gate spaces these concurrent refreshes at the cloud edge.
        await asyncio.gather(*(coordinator.async_refresh() for coordinator in coordinators))
        if not any(coordinator.last_update_success for coordinator in coordinators):
            raise ConfigEntryNotReady("No configured appliance could be reached")

        hass.data[DOMAIN][entry.entry_id] = MideaCloudRuntimeData(
            client, coordinators
        )
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await registry.release(account, cloud_name)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload entities and release the account session reference."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)
    registry: CloudSessionRegistry = hass.data[DOMAIN][DATA_SESSION_REGISTRY]
    await registry.release(entry.data[CONF_USERNAME], entry.data[CONF_CLOUD])
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after appliance selection or polling options change."""
    await hass.config_entries.async_reload(entry.entry_id)
