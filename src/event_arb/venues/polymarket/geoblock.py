"""Polymarket geographic-restriction check.

Source (verified 2026-08-21): <https://docs.polymarket.com/api-reference/geoblock>

    GET https://polymarket.com/api/geoblock
    -> {"blocked": bool, "ip": str, "country": str, "region": str}

This module exists to *enforce* the restriction, never to probe or work around it. It
**fails closed**: order submission is refused unless the endpoint affirmatively returns
``blocked == false``. A network error, a timeout, a non-200 status, a missing field, or
a non-boolean value all result in "blocked". A check that failed open would turn a
transient outage into a compliance violation.

The result is cached briefly so that a burst of checks does not hammer the endpoint, but
the cache is short and never used to satisfy a check after an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from event_arb.observability.logging import get_logger

__all__ = ["GeoblockChecker", "GeoblockStatus"]

log = get_logger(__name__)


@dataclass(frozen=True)
class GeoblockStatus:
    """Outcome of a geoblock check.

    ``allowed`` is the only field callers should branch on, and it is ``True`` only when
    the endpoint explicitly said ``blocked: false``.
    """

    allowed: bool
    country: str | None
    region: str | None
    reason: str
    checked_at: datetime

    @property
    def blocked(self) -> bool:
        return not self.allowed


def _blocked(reason: str) -> GeoblockStatus:
    return GeoblockStatus(
        allowed=False, country=None, region=None, reason=reason, checked_at=datetime.now(UTC)
    )


class GeoblockChecker:
    """Queries and caches the official Polymarket geoblock endpoint."""

    def __init__(
        self,
        url: str = "https://polymarket.com/api/geoblock",
        client: httpx.AsyncClient | None = None,
        cache_ttl: timedelta = timedelta(seconds=60),
        timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._client = client
        self._owns_client = client is None
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._cached: GeoblockStatus | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def check(self, force: bool = False) -> GeoblockStatus:
        """Return the current geoblock status, failing closed on any uncertainty."""
        now = datetime.now(UTC)
        cached = self._cached
        if (
            not force
            and cached is not None
            and cached.allowed
            and now - cached.checked_at < self._cache_ttl
        ):
            return cached

        client = await self._http()
        try:
            response = await client.get(self._url)
        except httpx.HTTPError as exc:
            status = _blocked(f"geoblock check failed: {type(exc).__name__}")
            log.warning("polymarket.geoblock.unreachable", reason=status.reason)
            self._cached = status
            return status

        if response.status_code != 200:
            status = _blocked(f"geoblock endpoint returned HTTP {response.status_code}")
            self._cached = status
            return status

        try:
            payload = response.json()
        except ValueError:
            status = _blocked("geoblock endpoint returned non-JSON")
            self._cached = status
            return status

        if not isinstance(payload, dict) or not isinstance(payload.get("blocked"), bool):
            status = _blocked("geoblock response missing boolean 'blocked'")
            self._cached = status
            return status

        is_blocked = bool(payload["blocked"])
        status = GeoblockStatus(
            allowed=not is_blocked,
            country=payload.get("country") if isinstance(payload.get("country"), str) else None,
            region=payload.get("region") if isinstance(payload.get("region"), str) else None,
            reason="allowed by geoblock endpoint" if not is_blocked else "blocked by venue",
            checked_at=datetime.now(UTC),
        )
        # The IP is deliberately not stored or logged.
        log.info(
            "polymarket.geoblock.checked",
            allowed=status.allowed,
            country=status.country,
            region=status.region,
        )
        self._cached = status
        return status

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
