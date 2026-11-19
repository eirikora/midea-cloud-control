"""Midea Cloud Control integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.device_registry import DeviceEntry

from .client import (
    CloudError,
    CloudInvalidAuthError,
    CloudProtocolError,
    MideaCloudClient,
)
from .const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_CLOUD,
    CONF_SCAN_INTERVAL,
    CONFIG_ENTRY_VERSION,
    DATA_SESSION_REGISTRY,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS,
    PLATFORMS,
)
from .coordinator import MideaCloudCoordinator, MideaCloudPollScheduler
from .log_filter import install_async_reply_log_filter, remove_async_reply_log_filter
from .session import CloudSessionRegistry, get_session_registry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MideaCloudRuntimeData:
    """Runtime resources owned by one config entry."""

    client: MideaCloudClient
    coordinators: list[MideaCloudCoordinator]
    poll_scheduler: MideaCloudPollScheduler


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate account schema and polling semantics without changing entity IDs."""
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer release; refuse instead of guessing its schema.
        _LOGGER.error(
            "Cannot downgrade Midea Cloud Control entry from schema version %s to %s",
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False
    if entry.version == CONFIG_ENTRY_VERSION:
        return True
    if entry.version not in (1, 2):
        _LOGGER.error(
            "Cannot migrate unsupported Midea Cloud Control schema version %s",
            entry.version,
        )
        return False

    data = dict(entry.data)
    options = dict(entry.options)
    if entry.version == 1:
        appliance = {
            CONF_APPLIANCE_ID: int(data.pop(CONF_APPLIANCE_ID)),
            CONF_APPLIANCE_NAME: data.pop(CONF_APPLIANCE_NAME),
        }
        data[CONF_APPLIANCES] = [appliance]

    # Version 2 used 90 seconds as a base plus a 0-30 second per-device
    # offset. Version 3 uses one fixed round-robin interval, so 105 seconds
    # preserves approximately the same account-wide request rate.
    if data.get(CONF_SCAN_INTERVAL, LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS) == (
        LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS
    ):
        data[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL_SECONDS
    if options.get(CONF_SCAN_INTERVAL) == LEGACY_DEFAULT_SCAN_INTERVAL_SECONDS:
        options[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL_SECONDS

    hass.config_entries.async_update_entry(
        entry, data=data, options=options, version=CONFIG_ENTRY_VERSION
    )
    _LOGGER.info("Migrated Midea Cloud Control entry to schema version 3")
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
    except ValueError as err:
        raise ConfigEntryError(
            "Conflicting credentials are configured for this cloud account; "
            "complete reauthentication for the account"
        ) from err
    except CloudProtocolError as err:
        raise ConfigEntryError(
            "Installed midea-local is incompatible with Midea Cloud Control"
        ) from err
    except CloudError as err:
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

    filter_installed = False
    try:
        install_async_reply_log_filter(hass)
        filter_installed = True
        # A single offline unit must not prevent the other entities loading.
        # The request gate spaces these concurrent refreshes at the cloud edge.
        await asyncio.gather(*(coordinator.async_refresh() for coordinator in coordinators))
        if not any(coordinator.last_update_success for coordinator in coordinators):
            raise ConfigEntryNotReady("No configured appliance could be reached")

        poll_scheduler = MideaCloudPollScheduler(hass, coordinators)
        hass.data[DOMAIN][entry.entry_id] = MideaCloudRuntimeData(
            client, coordinators, poll_scheduler
        )
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        poll_scheduler.start()
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if filter_installed:
            remove_async_reply_log_filter(hass)
        await registry.release(account, cloud_name)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload entities and release the account session reference."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime: MideaCloudRuntimeData | None = hass.data[DOMAIN].get(entry.entry_id)
    if runtime is not None:
        await runtime.poll_scheduler.async_stop()
    hass.data[DOMAIN].pop(entry.entry_id, None)
    registry: CloudSessionRegistry = hass.data[DOMAIN][DATA_SESSION_REGISTRY]
    await registry.release(entry.data[CONF_USERNAME], entry.data[CONF_CLOUD])
    remove_async_reply_log_filter(hass)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after appliance selection or polling options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removal only after an appliance is deselected from the entry."""
    del hass
    configured = entry.options.get(CONF_APPLIANCES, entry.data[CONF_APPLIANCES])
    configured_ids = {str(item[CONF_APPLIANCE_ID]) for item in configured}
    device_ids = {
        identifier
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
    }
    return configured_ids.isdisjoint(device_ids)
