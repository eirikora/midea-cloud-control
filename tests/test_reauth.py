"""Tests for account-wide config-flow reauthentication."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.midea_cloud_control import config_flow
from custom_components.midea_cloud_control.config_flow import MideaCloudConfigFlow
from custom_components.midea_cloud_control.const import CONF_CLOUD


@pytest.mark.asyncio
async def test_reauth_updates_every_entry_for_shared_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = SimpleNamespace(
        entry_id="primary",
        state=ConfigEntryState.SETUP_ERROR,
        data={
            CONF_USERNAME: "User@Example.invalid",
            CONF_PASSWORD: "old",
            CONF_CLOUD: "NetHome Plus",
        }
    )
    sibling = SimpleNamespace(
        entry_id="sibling",
        state=ConfigEntryState.LOADED,
        data={
            CONF_USERNAME: " user@example.invalid ",
            CONF_PASSWORD: "old",
            CONF_CLOUD: "NetHome Plus",
        }
    )
    unrelated = SimpleNamespace(
        entry_id="unrelated",
        state=ConfigEntryState.LOADED,
        data={
            CONF_USERNAME: "other@example.invalid",
            CONF_PASSWORD: "unchanged",
            CONF_CLOUD: "NetHome Plus",
        }
    )
    entries = [primary, sibling, unrelated]

    class FakeConfigEntries:
        def __init__(self) -> None:
            self.updates: list[tuple[object, dict[str, object]]] = []
            self.reloads: list[str] = []

        def async_update_entry(
            self, entry: object, *, data: dict[str, object], **_kwargs: object
        ) -> bool:
            entry.data = data  # type: ignore[attr-defined]
            self.updates.append((entry, data))
            return True

        def async_schedule_reload(self, entry_id: str) -> None:
            self.reloads.append(entry_id)

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.connect = AsyncMock()
            self.close = AsyncMock()

    manager = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=manager)
    registry = SimpleNamespace(update_credentials=AsyncMock())
    flow = MideaCloudConfigFlow()
    flow.hass = hass  # type: ignore[assignment]

    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: primary)
    monkeypatch.setattr(flow, "_async_current_entries", lambda: entries)
    monkeypatch.setattr(config_flow, "MideaCloudClient", FakeClient)
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda _hass: object())
    monkeypatch.setattr(config_flow, "get_session_registry", lambda _hass: registry)

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "new"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    registry.update_credentials.assert_awaited_once_with(
        "User@Example.invalid", "new", "NetHome Plus"
    )
    assert primary.state is ConfigEntryState.SETUP_ERROR
    assert [entry for entry, _data in manager.updates] == [sibling, primary]
    assert all(data[CONF_PASSWORD] == "new" for _entry, data in manager.updates)
    # The loaded sibling is reloaded by its update listener; only the entry in
    # SETUP_ERROR (which has no listener) needs an explicit reload.
    assert manager.reloads == ["primary"]
    assert unrelated.data[CONF_PASSWORD] == "unchanged"


def _entry(entry_id: str, state: ConfigEntryState, password: str) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        state=state,
        data={
            CONF_USERNAME: "user@example.invalid",
            CONF_PASSWORD: password,
            CONF_CLOUD: "NetHome Plus",
        },
    )


class _RecordingConfigEntries:
    """Mimic HA: async_update_entry returns whether data actually changed."""

    def __init__(self) -> None:
        self.reloads: list[str] = []

    def async_update_entry(
        self, entry: SimpleNamespace, *, data: dict[str, object], **_kwargs: object
    ) -> bool:
        changed = entry.data != data
        entry.data = data
        return changed

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


async def _run_reauth(
    monkeypatch: pytest.MonkeyPatch,
    primary: SimpleNamespace,
    entries: list[SimpleNamespace],
    password: str,
) -> _RecordingConfigEntries:
    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.connect = AsyncMock()
            self.close = AsyncMock()

    manager = _RecordingConfigEntries()
    flow = MideaCloudConfigFlow()
    flow.hass = SimpleNamespace(config_entries=manager)  # type: ignore[assignment]
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: primary)
    monkeypatch.setattr(flow, "_async_current_entries", lambda: entries)
    monkeypatch.setattr(config_flow, "MideaCloudClient", FakeClient)
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda _hass: object())
    monkeypatch.setattr(
        config_flow,
        "get_session_registry",
        lambda _hass: SimpleNamespace(update_credentials=AsyncMock()),
    )

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: password})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    return manager


@pytest.mark.asyncio
async def test_reauth_does_not_double_reload_loaded_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _entry("primary", ConfigEntryState.LOADED, "old")
    siblings = [
        _entry(f"sibling-{index}", ConfigEntryState.LOADED, "old") for index in range(15)
    ]

    manager = await _run_reauth(monkeypatch, primary, [primary, *siblings], "new")

    # Every loaded entry changed, so its own update listener performs the one
    # reload. Scheduling another would reload all 16 entries twice.
    assert manager.reloads == []
    assert all(e.data[CONF_PASSWORD] == "new" for e in [primary, *siblings])


@pytest.mark.asyncio
async def test_reauth_reloads_unchanged_and_failed_entries_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _entry("primary", ConfigEntryState.LOADED, "same")
    retrying = _entry("retrying", ConfigEntryState.SETUP_RETRY, "old")

    manager = await _run_reauth(monkeypatch, primary, [primary, retrying], "same")

    # Unchanged data fires no listener; a non-loaded entry has no listener.
    assert manager.reloads == ["retrying", "primary"]
