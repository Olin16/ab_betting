"""Polymarket read-only CLOB client.

Market-data endpoints only. No order-entry method exists on this class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from event_arb.config import PolymarketSettings
from event_arb.domain.enums import Outcome, Venue
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
from event_arb.venues.polymarket.geoblock import GeoblockChecker, GeoblockStatus
from event_arb.venues.polymarket.mapper import build_book, parse_market

__all__ = ["PolymarketClient"]

log = get_logger(__name__)


class PolymarketClient(VenueAdapter):
    """Read-only Polymarket adapter."""

    def __init__(
        self,
        settings: PolymarketSettings,
        client: httpx.AsyncClient | None = None,
        geoblock: GeoblockChecker | None = None,
    ) -> None:
        super().__init__(Venue.POLYMARKET)
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        self._limiter = RateLimiter(settings.requests_per_second)
        self._geoblock = geoblock or GeoblockChecker(settings.geoblock_url, client=client)
        #: condition_id -> {"YES": token_id, "NO": token_id}
        self._token_ids: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------ compliance

    async def check_geoblock(self, force: bool = False) -> GeoblockStatus:
        """Query the official geoblock endpoint. Fails closed."""
        return await self._geoblock.check(force=force)

    # ------------------------------------------------------------------- internals

    async def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._limiter.acquire()
        try:
            response = await self._client.get(f"{base}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise TransientError(f"Polymarket timeout on {path}", Venue.POLYMARKET) from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"Polymarket transport error on {path}", Venue.POLYMARKET) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                f"Polymarket rate limited on {path}",
                Venue.POLYMARKET,
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code >= 500:
            raise TransientError(f"Polymarket {response.status_code} on {path}", Venue.POLYMARKET)
        if response.status_code >= 400:
            raise MarketDataError(f"Polymarket {response.status_code} on {path}", Venue.POLYMARKET)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError(
                f"Polymarket non-JSON response on {path}", Venue.POLYMARKET
            ) from exc
        self.health.record_message()
        return payload

    def _remember_tokens(self, market: VenueMarket) -> None:
        tokens = market.raw_metadata.get("tokens")
        if not isinstance(tokens, list):
            return
        mapping: dict[str, str] = {}
        for token in tokens:
            if not isinstance(token, dict):
                continue
            outcome = str(token.get("outcome", "")).upper()
            token_id = token.get("token_id")
            if outcome in {"YES", "NO"} and isinstance(token_id, str):
                mapping[outcome] = token_id
        if mapping:
            self._token_ids[market.venue_market_id] = mapping

    def token_id_for(self, market_id: str, outcome: Outcome) -> str | None:
        return self._token_ids.get(market_id, {}).get(outcome.value)

    # --------------------------------------------------------------------- adapter

    async def discover_markets(self, limit: int | None = None) -> list[VenueMarket]:
        params: dict[str, Any] = {"active": "true", "closed": "false"}
        if limit is not None:
            params["limit"] = limit
        payload = await self._get(self._settings.clob_base_url, "/markets", params=params)
        entries = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise MarketDataError("Polymarket /markets did not return a list", Venue.POLYMARKET)

        markets: list[VenueMarket] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                market = parse_market(entry)
            except MarketDataError as exc:
                log.warning("polymarket.market.parse_failed", error=str(exc))
                continue
            self._remember_tokens(market)
            markets.append(market)
        return markets

    async def fetch_orderbook(self, market_id: str) -> OrderBook:
        """Fetch both outcome-token books and normalize them into one book."""
        tokens = self._token_ids.get(market_id)
        if not tokens:
            raise MarketDataError(
                f"unknown Polymarket token ids for market {market_id}; "
                "call discover_markets() first",
                Venue.POLYMARKET,
            )

        yes_token = tokens.get("YES")
        if yes_token is None:
            raise MarketDataError(
                f"Polymarket market {market_id} has no YES token", Venue.POLYMARKET
            )
        yes_payload = await self._get(
            self._settings.clob_base_url, "/book", params={"token_id": yes_token}
        )
        no_payload: dict[str, Any] | None = None
        no_token = tokens.get("NO")
        if no_token is not None:
            fetched = await self._get(
                self._settings.clob_base_url, "/book", params={"token_id": no_token}
            )
            if isinstance(fetched, dict):
                no_payload = fetched

        if not isinstance(yes_payload, dict):
            raise MarketDataError("Polymarket /book returned a non-object", Venue.POLYMARKET)
        return build_book(market_id, yes_payload, no_payload, received_at=datetime.now(UTC))

    def stream(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        from event_arb.venues.polymarket.websocket import PolymarketWebSocketStream

        token_map = {market_id: self._token_ids.get(market_id, {}) for market_id in market_ids}
        return PolymarketWebSocketStream(
            settings=self._settings, health=self.health, token_map=token_map
        ).run(market_ids)

    async def close(self) -> None:
        await self._geoblock.close()
        if self._owns_client:
            await self._client.aclose()
