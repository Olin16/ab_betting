"""Kalshi read-only REST client.

Only market-data endpoints are implemented. There is no order-entry method on this class
at all — not a disabled one, not a private one. Live trading is a separate, unimplemented
subsystem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from event_arb.config import KalshiSettings
from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook, VenueMarket
from event_arb.observability.logging import get_logger
from event_arb.venues.base import (
    AuthenticationError,
    MarketDataError,
    RateLimiter,
    RateLimitError,
    StreamEvent,
    TransientError,
    VenueAdapter,
)
from event_arb.venues.kalshi.auth import KalshiSigner
from event_arb.venues.kalshi.mapper import parse_market, parse_orderbook

__all__ = ["KalshiClient"]

log = get_logger(__name__)

_PATH_PREFIX = "/trade-api/v2"


class KalshiClient(VenueAdapter):
    """Read-only Kalshi adapter."""

    def __init__(
        self,
        settings: KalshiSettings,
        client: httpx.AsyncClient | None = None,
        signer: KalshiSigner | None = None,
    ) -> None:
        super().__init__(Venue.KALSHI)
        self._settings = settings
        self._base_url = settings.rest_base_url
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        self._limiter = RateLimiter(settings.requests_per_second)

        # Signing is optional: the docs disagree about whether market data requires auth
        # (see docs/api-assumptions.md A4), so sign when we can and proceed when we can't.
        if signer is not None:
            self._signer: KalshiSigner | None = signer
        elif settings.has_credentials and settings.private_key_path is not None:
            self._signer = KalshiSigner.from_path(settings.api_key_id, settings.private_key_path)
        else:
            self._signer = None

    # ------------------------------------------------------------------ internals

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if self._signer is None:
            return {}
        return self._signer.headers(method, f"{_PATH_PREFIX}{path}")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._limiter.acquire()
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.get(
                url, params=params, headers=self._auth_headers("GET", path)
            )
        except httpx.TimeoutException as exc:
            raise TransientError(f"Kalshi timeout on {path}", Venue.KALSHI) from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"Kalshi transport error on {path}", Venue.KALSHI) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                f"Kalshi rate limited on {path}",
                Venue.KALSHI,
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code in (401, 403):
            raise AuthenticationError(f"Kalshi auth rejected on {path}", Venue.KALSHI)
        if response.status_code >= 500:
            raise TransientError(f"Kalshi {response.status_code} on {path}", Venue.KALSHI)
        if response.status_code >= 400:
            raise MarketDataError(f"Kalshi {response.status_code} on {path}", Venue.KALSHI)

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise MarketDataError(f"Kalshi non-JSON response on {path}", Venue.KALSHI) from exc
        self.health.record_message()
        return payload

    # -------------------------------------------------------------------- adapter

    async def discover_markets(self, limit: int | None = None) -> list[VenueMarket]:
        params: dict[str, Any] = {"status": "open"}
        if limit is not None:
            params["limit"] = min(limit, 1000)
        payload = await self._get("/markets", params=params)
        raw_markets = payload.get("markets", [])
        if not isinstance(raw_markets, list):
            raise MarketDataError("Kalshi /markets did not return a list", Venue.KALSHI)

        markets: list[VenueMarket] = []
        for entry in raw_markets:
            if not isinstance(entry, dict):
                continue
            try:
                markets.append(parse_market(entry))
            except MarketDataError as exc:
                # One malformed market must not blind the scanner to every other market.
                log.warning("kalshi.market.parse_failed", error=str(exc))
        return markets

    async def fetch_orderbook(self, market_id: str) -> OrderBook:
        payload = await self._get(f"/markets/{market_id}/orderbook")
        return parse_orderbook(market_id, payload, received_at=datetime.now(UTC))

    def stream(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        """Stream normalized book updates over the Kalshi WebSocket."""
        from event_arb.venues.kalshi.websocket import KalshiWebSocketStream

        return KalshiWebSocketStream(
            settings=self._settings, health=self.health, signer=self._signer
        ).run(market_ids)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
