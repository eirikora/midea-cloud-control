"""Config schema migration and account identity tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.midea_cloud_control import async_migrate_entry
from custom_components.midea_cloud_control.config_flow import account_unique_id
from custom_components.midea_cloud_control.const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_SCAN_INTERVAL,
)


def test_account_unique_id_is_normalized_and_does_not_expose_account() -> None:
    first = account_unique_id("NetHome Plus", " User@Example.invalid ")
    second = account_unique_id("NetHome Plus", "user@example.invalid")

    assert first == second
    assert "user" not in first


@pytest.mark.asyncio
async def test_v1_migration_preserves_appliance_identity() -> None:
    updates: dict[str, object] = {}

    class FakeConfigEntries:
        def async_update_entry(self, _entry, **kwargs: object) -> None:
            updates.update(kwargs)

    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(
        version=1,
        data={
            "username": "user@example.invalid",
            "password": "not-real",
            "cloud": "NetHome Plus",
            CONF_APPLIANCE_ID: 123,
            CONF_APPLIANCE_NAME: "Bedroom",
        },
    )

    assert await async_migrate_entry(hass, entry)  # type: ignore[arg-type]
    assert updates["version"] == 2
    data = updates["data"]
    assert isinstance(data, dict)
    assert data[CONF_APPLIANCES] == [
        {CONF_APPLIANCE_ID: 123, CONF_APPLIANCE_NAME: "Bedroom"}
    ]
    assert data[CONF_SCAN_INTERVAL] == 90
