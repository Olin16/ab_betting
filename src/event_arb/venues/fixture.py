"""Fixture-backed venue adapter.

This is the **default** data source. It implements the same
:class:`~event_arb.venues.base.VenueAdapter` interface as the live clients, replaying
recorded or synthetic payloads through the *real* mappers. That matters: fixtures
exercise the actual Kalshi/Polymarket parsing code, so a schema-handling bug is caught by
the test suite rather than only in production.

Fixture layout::

    tests/fixtures/
      kalshi/markets.json          # {"markets": [...]}
      kalshi/books/<TICKER>.json   # {"orderbook_fp": {...}} or a list of timed frames
      polymarket/markets.json      # {"data": [...]}
      polymarket/books/<id>.json   # {"yes": {...}, "no": {...}} or timed frames
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from event_arb.domain.enums import Venue, VenueProduct
from event_arb.domain.models import OrderBook, VenueMarket
from event_arb.venues.base import MarketDataError, StreamEvent, VenueAdapter
from event_arb.venues.kalshi.mapper import parse_market as parse_kalshi_market
from event_arb.venues.kalshi.mapper import parse_orderbook as parse_kalshi_book
from event_arb.venues.polymarket.mapper import build_book as build_poly_book
from event_arb.venues.polymarket.mapper import parse_market as parse_poly_market
from event_arb.venues.polymarket_us.mapper import parse_book as parse_us_book
from event_arb.venues.polymarket_us.mapper import parse_market as parse_us_market

__all__ = ["FixtureAdapter", "load_json"]


def _fixture_subdir(venue: Venue, product: VenueProduct) -> str:
    if venue is Venue.KALSHI:
        return "kalshi"
    return "polymarket_us" if product is VenueProduct.POLYMARKET_US else "polymarket"


def load_json(path: Path) -> Any:
    """Load a fixture keeping every number exact.

    ``parse_float=Decimal`` mirrors the live clients. Without it a fixture price becomes a
    binary float at load time, which both defeats the Decimal discipline and diverges from
    the code path the fixture is supposed to be exercising.
    """
    try:
        return json.loads(path.read_text(), parse_float=Decimal)
    except FileNotFoundError as exc:
        raise MarketDataError(f"fixture not found: {path}") from exc
    except ValueError as exc:
        raise MarketDataError(f"invalid JSON fixture: {path}") from exc


class FixtureAdapter(VenueAdapter):
    """Replays recorded payloads as if they came from the venue."""

    def __init__(
        self,
        venue: Venue,
        fixture_dir: Path,
        frame_interval: float = 0.0,
        product: VenueProduct = VenueProduct.POLYMARKET_GLOBAL,
    ) -> None:
        super().__init__(venue)
        # Product selects the corpus: Polymarket US payloads differ from Global ones in
        # schema, identifiers and settlement asset, so serving one while configured for
        # the other would test the wrong adapter entirely.
        self._product = product
        self._dir = fixture_dir / _fixture_subdir(venue, product)
        self._frame_interval = frame_interval
        #: Overrides the receipt timestamp so tests can synthesize stale books.
        self.received_at_override: datetime | None = None
        #: Keep raw recorded exchange timestamps (used to replay clock-skew scenarios).
        self.preserve_exchange_timestamps = False
        #: Simulated venue-to-local lag applied when rebasing recorded timestamps.
        self.replay_exchange_lag = timedelta(milliseconds=50)

    @property
    def discovery_base_url(self) -> str:
        """No network is involved, and the plan says so rather than showing a live host."""
        return f"(fixture corpus: {self._dir})"

    @property
    def sends_credentials(self) -> bool:
        return False

    # ------------------------------------------------------------------- internals

    def _book_path(self, market_id: str) -> Path:
        safe = market_id.replace("/", "_")
        return self._dir / "books" / f"{safe}.json"

    def _frames(self, market_id: str) -> list[dict[str, Any]]:
        raw = load_json(self._book_path(market_id))
        if isinstance(raw, list):
            return [frame for frame in raw if isinstance(frame, dict)]
        if isinstance(raw, dict):
            return [raw]
        raise MarketDataError(f"unsupported book fixture shape for {market_id}")

    def _to_book(self, market_id: str, frame: dict[str, Any]) -> OrderBook:
        received = self.received_at_override or datetime.now(UTC)
        if self.venue is Venue.KALSHI:
            book = parse_kalshi_book(market_id, frame, received_at=received)
        elif self._product is VenueProduct.POLYMARKET_US:
            book = parse_us_book(market_id, frame, received_at=received)
        else:
            yes = frame.get("yes")
            if not isinstance(yes, dict):
                raise MarketDataError(f"Polymarket book fixture {market_id} missing 'yes'")
            no = frame.get("no") if isinstance(frame.get("no"), dict) else None
            book = build_poly_book(market_id, yes, no, received_at=received)
        return self._rebase(book, received)

    def _rebase(self, book: OrderBook, received: datetime) -> OrderBook:
        """Rebase a recorded exchange timestamp onto replay time.

        A recording replayed later has an exchange timestamp that is arbitrarily far from
        now, which the clock-skew check would (correctly) treat as an unusable feed. The
        recorded instant carries no meaning at replay time, so the *offset* is preserved
        while the absolute position is moved to now. Set ``preserve_exchange_timestamps``
        to keep the raw values when replaying skew scenarios deliberately.
        """
        if self.preserve_exchange_timestamps or book.exchange_timestamp is None:
            return book
        return book.model_copy(update={"exchange_timestamp": received - self.replay_exchange_lag})

    # --------------------------------------------------------------------- adapter

    async def discover_markets(
        self, limit: int | None = None, market_ids: list[str] | None = None
    ) -> list[VenueMarket]:
        raw = load_json(self._dir / "markets.json")
        entries = raw.get("markets", raw.get("data", [])) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise MarketDataError(f"{self.venue.value} markets fixture is not a list")

        if self.venue is Venue.KALSHI:
            parse = parse_kalshi_market
        elif self._product is VenueProduct.POLYMARKET_US:
            parse = parse_us_market
        else:
            parse = parse_poly_market
        markets = [parse(entry) for entry in entries if isinstance(entry, dict)]
        if market_ids:
            wanted = set(market_ids)
            markets = [m for m in markets if m.venue_market_id in wanted]
        self.health.record_message()
        self.health.connected = True
        return markets[:limit] if limit is not None else markets

    async def fetch_orderbook(self, market_id: str) -> OrderBook:
        frames = self._frames(market_id)
        if not frames:
            raise MarketDataError(f"empty book fixture for {market_id}")
        self.health.record_message()
        self.health.connected = True
        return self._to_book(market_id, frames[0])

    async def stream(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        """Replay every fixture frame for each market, in order."""
        self.health.connected = True
        self.health.subscribed_markets = set(market_ids)
        for market_id in market_ids:
            for index, frame in enumerate(self._frames(market_id)):
                if self._frame_interval:
                    await asyncio.sleep(self._frame_interval)
                self.health.record_message()
                yield StreamEvent(
                    venue=self.venue,
                    market_id=market_id,
                    book=self._to_book(market_id, frame),
                    is_snapshot=index == 0,
                )
