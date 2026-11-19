"""Tests for non-sensitive Home Assistant diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.midea_cloud_control.const import DOMAIN
from custom_components.midea_cloud_control.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_redact_credentials_and_include_health() -> None:
    appliance_id = 123
    last_success = datetime(2026, 11, 19, tzinfo=UTC)
    coordinator = SimpleNamespace(
        appliance_id=appliance_id,
        last_success_at=last_success,
        consecutive_failures=2,
    )
    client = SimpleNamespace(
        generation=3,
        appliance_diagnostics=lambda _appliance_id: {
            "missing_replies": 4,
            "online": True,
        },
    )
    runtime = SimpleNamespace(client=client, coordinators=[coordinator])
    entry = SimpleNamespace(
        entry_id="entry",
        as_dict=lambda: {
            "data": {
                "username": "user@example.invalid",
                "password": "secret",
            }
        },
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})

    result = await async_get_config_entry_diagnostics(
        hass, entry  # type: ignore[arg-type]
    )

    config_data = result["config_entry"]["data"]
    assert config_data["username"] == "**REDACTED**"
    assert config_data["password"] == "**REDACTED**"
    assert result["session_generation"] == 3
    assert result["appliances"] == [
        {
            "appliance_id": appliance_id,
            "last_success_at": last_success.isoformat(),
            "consecutive_failures": 2,
            "missing_replies": 4,
            "online": True,
        }
    ]
