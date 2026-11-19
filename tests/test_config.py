"""Config schema migration and account identity tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryError

import custom_components.midea_cloud_control as integration
from custom_components.midea_cloud_control import (
    async_migrate_entry,
    async_remove_config_entry_device,
)
from custom_components.midea_cloud_control.client import CloudProtocolError
from custom_components.midea_cloud_control.climate import MideaCloudClimate
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
        options={},
    )

    assert await async_migrate_entry(hass, entry)  # type: ignore[arg-type]
    assert updates["version"] == 3
    data = updates["data"]
    assert isinstance(data, dict)
    assert data[CONF_APPLIANCES] == [
        {CONF_APPLIANCE_ID: 123, CONF_APPLIANCE_NAME: "Bedroom"}
    ]
    assert data[CONF_SCAN_INTERVAL] == 105


@pytest.mark.asyncio
async def test_v2_migration_preserves_previous_average_poll_rate() -> None:
    updates: dict[str, object] = {}

    class FakeConfigEntries:
        def async_update_entry(self, _entry, **kwargs: object) -> None:
            updates.update(kwargs)

    entry = SimpleNamespace(
        version=2,
        data={
            CONF_APPLIANCES: [
                {CONF_APPLIANCE_ID: 123, CONF_APPLIANCE_NAME: "Bedroom"}
            ],
            CONF_SCAN_INTERVAL: 90,
        },
        options={CONF_SCAN_INTERVAL: 90},
    )

    assert await async_migrate_entry(  # type: ignore[arg-type]
        SimpleNamespace(config_entries=FakeConfigEntries()), entry
    )
    assert updates["version"] == 3
    assert updates["data"][CONF_SCAN_INTERVAL] == 105  # type: ignore[index]
    assert updates["options"][CONF_SCAN_INTERVAL] == 105  # type: ignore[index]


@pytest.mark.asyncio
async def test_current_version_needs_no_migration() -> None:
    manager = SimpleNamespace(async_update_entry=lambda *_a, **_k: None)
    entry = SimpleNamespace(version=3, data={}, options={})

    assert await async_migrate_entry(  # type: ignore[arg-type]
        SimpleNamespace(config_entries=manager), entry
    )


@pytest.mark.asyncio
async def test_newer_schema_version_is_refused() -> None:
    updates: list[object] = []
    manager = SimpleNamespace(async_update_entry=lambda *a, **_k: updates.append(a))
    entry = SimpleNamespace(version=4, data={}, options={})

    assert not await async_migrate_entry(  # type: ignore[arg-type]
        SimpleNamespace(config_entries=manager), entry
    )
    assert updates == []


@pytest.mark.asyncio
async def test_unknown_older_schema_version_is_refused() -> None:
    updates: list[object] = []
    manager = SimpleNamespace(async_update_entry=lambda *a, **_k: updates.append(a))
    entry = SimpleNamespace(version=0, data={}, options={})

    assert not await async_migrate_entry(  # type: ignore[arg-type]
        SimpleNamespace(config_entries=manager), entry
    )
    assert updates == []


@pytest.mark.asyncio
async def test_incompatible_midea_local_is_permanent_entry_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SimpleNamespace(
        acquire=AsyncMock(
            side_effect=CloudProtocolError("missing private cloud API")
        )
    )
    entry = SimpleNamespace(
        data={
            "username": "user@example.invalid",
            "password": "not-real",
            "cloud": "NetHome Plus",
        }
    )
    monkeypatch.setattr(integration, "get_session_registry", lambda _hass: registry)

    with pytest.raises(ConfigEntryError, match="incompatible"):
        await integration.async_setup_entry(  # type: ignore[arg-type]
            SimpleNamespace(), entry
        )


def test_climate_unique_id_depends_only_on_appliance_id() -> None:
    original = SimpleNamespace(
        appliance_id=123,
        appliance_name="Bedroom",
        data=None,
        last_update_success=True,
    )
    renamed = SimpleNamespace(
        appliance_id=123,
        appliance_name="Camera da letto",
        data=None,
        last_update_success=True,
    )

    original_entity = MideaCloudClimate(original, SimpleNamespace())  # type: ignore[arg-type]
    renamed_entity = MideaCloudClimate(renamed, SimpleNamespace())  # type: ignore[arg-type]

    assert original_entity.unique_id == "123"
    assert renamed_entity.unique_id == original_entity.unique_id


@pytest.mark.asyncio
async def test_device_can_only_be_removed_after_it_is_deselected() -> None:
    entry = SimpleNamespace(
        data={
            CONF_APPLIANCES: [
                {CONF_APPLIANCE_ID: 123, CONF_APPLIANCE_NAME: "Bedroom"}
            ]
        },
        options={},
    )
    selected = SimpleNamespace(identifiers={("midea_cloud_control", "123")})
    deselected = SimpleNamespace(identifiers={("midea_cloud_control", "999")})

    assert not await async_remove_config_entry_device(
        AsyncMock(), entry, selected  # type: ignore[arg-type]
    )
    assert await async_remove_config_entry_device(
        AsyncMock(), entry, deselected  # type: ignore[arg-type]
    )
