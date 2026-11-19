"""Tests for shared-session reference counting."""

from __future__ import annotations

import asyncio
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from custom_components.midea_cloud_control.session import CloudSessionRegistry


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.connect_calls = 0
        self.close_calls = 0
        self.instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def clear_fake_clients() -> None:
    FakeClient.instances.clear()


@pytest.mark.asyncio
async def test_one_device_uses_one_session() -> None:
    registry = CloudSessionRegistry(object(), client_factory=FakeClient)  # type: ignore[arg-type]

    client = await registry.acquire("user@example.invalid", "pw", "NetHome Plus")

    assert registry.active_sessions == 1
    assert client is FakeClient.instances[0]
    assert await registry.references("user@example.invalid", "NetHome Plus") == 1


@pytest.mark.asyncio
async def test_sixteen_devices_share_one_session() -> None:
    registry = CloudSessionRegistry(object(), client_factory=FakeClient)  # type: ignore[arg-type]

    clients = await asyncio.gather(
        *(
            registry.acquire("User@Example.invalid", "pw", "NetHome Plus")
            for _ in range(16)
        )
    )

    assert len(FakeClient.instances) == 1
    assert len({id(client) for client in clients}) == 1
    assert await registry.references("user@example.invalid", "NetHome Plus") == 16


@pytest.mark.asyncio
async def test_release_keeps_shared_client_until_last_device() -> None:
    registry = CloudSessionRegistry(object(), client_factory=FakeClient)  # type: ignore[arg-type]
    await registry.acquire("user@example.invalid", "pw", "NetHome Plus")
    await registry.acquire("user@example.invalid", "pw", "NetHome Plus")
    client = FakeClient.instances[0]

    await registry.release("user@example.invalid", "NetHome Plus")
    assert registry.active_sessions == 1
    assert client.close_calls == 0

    await registry.release("user@example.invalid", "NetHome Plus")
    assert registry.active_sessions == 0
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_temporary_lease_preserves_persistent_session() -> None:
    registry = CloudSessionRegistry(object(), client_factory=FakeClient)  # type: ignore[arg-type]
    persistent = await registry.acquire("user@example.invalid", "pw", "NetHome Plus")

    async with registry.lease(
        "user@example.invalid", "pw", "NetHome Plus"
    ) as temporary:
        assert temporary is persistent
        assert await registry.references("user@example.invalid", "NetHome Plus") == 2

    assert await registry.references("user@example.invalid", "NetHome Plus") == 1
    assert persistent.close_calls == 0


@pytest.mark.asyncio
async def test_reauth_updates_active_shared_credentials() -> None:
    registry = CloudSessionRegistry(object(), client_factory=FakeClient)  # type: ignore[arg-type]
    client = await registry.acquire("user@example.invalid", "old", "NetHome Plus")
    client.update_password = AsyncMock()  # type: ignore[method-assign]

    await registry.update_credentials(
        "user@example.invalid", "new", "NetHome Plus"
    )

    client.update_password.assert_awaited_once_with("new")
    same_client = await registry.acquire(
        "user@example.invalid", "new", "NetHome Plus"
    )
    assert same_client is client
