"""Diagnostics support for Midea Cloud Control."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import MideaCloudRuntimeData
from .const import DOMAIN

_TO_REDACT = {"password", "username"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return non-sensitive session and appliance health information."""
    runtime: MideaCloudRuntimeData = hass.data[DOMAIN][entry.entry_id]
    appliances = []
    for coordinator in runtime.coordinators:
        appliances.append(
            {
                "appliance_id": coordinator.appliance_id,
                "last_success_at": (
                    coordinator.last_success_at.isoformat()
                    if coordinator.last_success_at is not None
                    else None
                ),
                "consecutive_failures": coordinator.consecutive_failures,
                **runtime.client.appliance_diagnostics(coordinator.appliance_id),
            }
        )
    return {
        "config_entry": async_redact_data(entry.as_dict(), _TO_REDACT),
        "session_generation": runtime.client.generation,
        "appliances": appliances,
    }
