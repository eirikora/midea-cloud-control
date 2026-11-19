"""Shared cloud-session lifecycle for Midea Cloud Control."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import MideaCloudClient
from .const import (
    DATA_SESSION_REGISTRY,
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MIN_REQUEST_INTERVAL,
    DOMAIN,
)


def normalize_account(account: str) -> str:
    """Return a stable in-memory key without changing login credentials."""
    return account.strip().casefold()


@dataclass(slots=True)
class _SessionRecord:
    client: MideaCloudClient
    password_digest: bytes
    references: int = 1


class CloudSessionRegistry:
    """Keep exactly one logical cloud client per account and cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        client_factory: Callable[..., MideaCloudClient] = MideaCloudClient,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self._session = session
        self._client_factory = client_factory
        self._max_concurrent_requests = max_concurrent_requests
        self._min_request_interval = min_request_interval
        self._records: dict[tuple[str, str], _SessionRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def key(cloud_name: str, account: str) -> tuple[str, str]:
        """Build a non-loggable registry key."""
        return cloud_name, normalize_account(account)

    @property
    def active_sessions(self) -> int:
        """Return the number of shared clients, primarily for diagnostics/tests."""
        return len(self._records)

    async def acquire(
        self, account: str, password: str, cloud_name: str
    ) -> MideaCloudClient:
        """Acquire and authenticate the shared client for an account."""
        key = self.key(cloud_name, account)
        digest = hashlib.sha256(password.encode()).digest()

        async with self._lock:
            if record := self._records.get(key):
                if not hmac.compare_digest(record.password_digest, digest):
                    raise ValueError(
                        "Conflicting credentials are configured for this cloud account"
                    )
                record.references += 1
                client = record.client
            else:
                client = self._client_factory(
                    account,
                    password,
                    cloud_name,
                    session=self._session,
                    max_concurrent_requests=self._max_concurrent_requests,
                    min_request_interval=self._min_request_interval,
                )
                self._records[key] = _SessionRecord(client, digest)

        try:
            await client.connect()
        except Exception:
            await self.release(account, cloud_name)
            raise
        return client

    async def release(self, account: str, cloud_name: str) -> None:
        """Release a client and close it after its last config entry unloads."""
        key = self.key(cloud_name, account)
        client: MideaCloudClient | None = None
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.references -= 1
            if record.references <= 0:
                client = record.client
                del self._records[key]

        if client is not None:
            await client.close()

    @asynccontextmanager
    async def lease(
        self, account: str, password: str, cloud_name: str
    ) -> AsyncIterator[MideaCloudClient]:
        """Temporarily share a client without disturbing persistent users."""
        client = await self.acquire(account, password, cloud_name)
        try:
            yield client
        finally:
            await self.release(account, cloud_name)

    async def references(self, account: str, cloud_name: str) -> int:
        """Return a session's reference count, primarily for tests."""
        async with self._lock:
            record = self._records.get(self.key(cloud_name, account))
            return record.references if record else 0


def get_session_registry(hass: HomeAssistant) -> CloudSessionRegistry:
    """Return the one central cloud-session registry for Home Assistant."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get(DATA_SESSION_REGISTRY)
    if registry is None:
        registry = CloudSessionRegistry(async_get_clientsession(hass))
        domain_data[DATA_SESSION_REGISTRY] = registry
    return registry
