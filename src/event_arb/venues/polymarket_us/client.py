"""Polymarket US public read-only REST client.

Uses the **public gateway only** (``https://gateway.polymarket.us``), which the
documentation states needs no API key: *"Use the public API to browse what's available on
Polymarket US. No API key needed."*

This client therefore holds **no credentials at all**. The authenticated API
(``https://api.polymarket.us``) — which is where the WebSocket lives — is deliberately not
reached from here. Streaming is a later, separately-authenticated enhancement; until then
ingestion is REST polling, which is what makes a credential-free trial possible.

There is no order-entry method on this class, and none may be added.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from event_arb.config import PolymarketSettings
from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook, VenueMarket
from event_arb.observability.logging import get_logger
from event_arb.venues.base import (
    MarketDataError,
    RateLimiter,
    RateLimitError,
    StreamEvent,
    TransientError,
    VenueAdapter,
)
from event_arb.venues.polymarket_us.mapper import parse_book, parse_market, parse_payload

__all__ = ["PolymarketUSClient"]

log = get_logger(__name__)


class PolymarketUSClient(VenueAdapter):
    """Read-only Polymarket US adapter over the public gateway."""

    def __init__(
        self,
        settings: PolymarketSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(Venue.POLYMARKET)
        self._settings = settings
        self._base_url = settings.us_gateway_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        # Documented at 20 req/s per IP; the configured default is well under that. A rate
        # limit is a term of use, not a performance target.
        self._limiter = RateLimiter(settings.requests_per_second)

    # ------------------------------------------------------------------- internals

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire()
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Polymarket US timeout on {path}", Venue.POLYMARKET) from exc
        except httpx.HTTPError as exc:
            raise TransientError(
                f"Polymarket US transport error on {path}", Venue.POLYMARKET
            ) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                f"Polymarket US rate limited on {path}",
                Venue.POLYMARKET,
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code >= 500:
            raise TransientError(
                f"Polymarket US {response.status_code} on {path}", Venue.POLYMARKET
            )
        if response.status_code >= 400:
            raise MarketDataError(
                f"Polymarket US {response.status_code} on {path}", Venue.POLYMARKET
            )

        payload = parse_payload(response.text)
        self.health.record_message()
        return payload

    @staticmethod
    def _entries(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            data = data.get("markets", [])
        if not isinstance(data, list):
            raise MarketDataError("Polymarket US market list is not an array", Venue.POLYMARKET)
        return [entry for entry in data if isinstance(entry, dict)]

    # --------------------------------------------------------------------- adapter

    async def discover_markets(
        self, limit: int | None = None, market_ids: list[str] | None = None
    ) -> list[VenueMarket]:
        if market_ids:
            return await self._fetch_markets_by_slug(market_ids)

        params: dict[str, Any] = {"active": "true", "closed": "false"}
        if limit is not None:
            params["limit"] = limit
        payload = await self._get("/v1/markets", params=params)

        markets: list[VenueMarket] = []
        for entry in self._entries(payload):
            try:
                markets.append(parse_market(entry))
            except MarketDataError as exc:
                # One malformed market must not blind the scanner to the rest.
                log.warning("polymarket_us.market.parse_failed", error=str(exc))
        return markets

    async def _fetch_markets_by_slug(self, slugs: list[str]) -> list[VenueMarket]:
        """Targeted retrieval: fetch only the markets a trial actually watches."""
        markets: list[VenueMarket] = []
        for slug in slugs:
            try:
                payload = await self._get(f"/v1/market/slug/{slug}")
            except MarketDataError as exc:
                log.warning("polymarket_us.market.fetch_failed", slug=slug, error=str(exc))
                continue
            entry = payload.get("data", payload) if isinstance(payload, dict) else payload
            if not isinstance(entry, dict):
                log.warning("polymarket_us.market.unexpected_shape", slug=slug)
                continue
            try:
                markets.append(parse_market(entry))
            except MarketDataError as exc:
                log.warning("polymarket_us.market.parse_failed", slug=slug, error=str(exc))
        return markets

    async def fetch_orderbook(self, market_id: str) -> OrderBook:
        """Fetch the full depth book for one market."""
        payload = await self._get(f"/v1/markets/{market_id}/book")
        if not isinstance(payload, dict):
            raise MarketDataError("Polymarket US book is not an object", Venue.POLYMARKET)
        body = payload.get("data", payload)
        if not isinstance(body, dict):
            raise MarketDataError("Polymarket US book data is not an object", Venue.POLYMARKET)
        return parse_book(market_id, body, received_at=datetime.now(UTC))

    def stream(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        """Not available without credentials.

        The Polymarket US WebSocket lives on the authenticated API. Raising here rather
        than silently returning an empty stream keeps a caller from believing it has live
        ingestion when it has nothing; REST polling is the supported credential-free path.
        """
        raise NotImplementedError(
            "Polymarket US streaming requires the authenticated API "
            "(https://api.polymarket.us); use REST polling ingestion instead"
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
