"""Replay of recorded book frames through the adapter streaming path."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from event_arb.domain.enums import Venue
from event_arb.orderbooks.book import BookStore
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.orderbooks.synchronizer import BookSynchronizer
from event_arb.venues.fixture import FixtureAdapter
from tests.conftest import KALSHI_FED, POLY_FED

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def test_replay_applies_every_recorded_frame_in_order() -> None:
    adapter = FixtureAdapter(Venue.KALSHI, FIXTURES)
    events = [event async for event in adapter.stream([KALSHI_FED])]

    assert len(events) == 2
    assert events[0].is_snapshot is True
    assert events[1].is_snapshot is False
    # Frame 2 reduces the best YES bid size from 120 to 90.
    assert events[0].book.yes.bids[0].quantity == Decimal("120.00")
    assert events[1].book.yes.bids[0].quantity == Decimal("90.00")


async def test_replay_of_polymarket_frames_updates_ask_depth() -> None:
    adapter = FixtureAdapter(Venue.POLYMARKET, FIXTURES)
    events = [event async for event in adapter.stream([POLY_FED])]

    assert len(events) == 2
    assert events[0].book.no.asks[0].quantity == Decimal("200")
    assert events[1].book.no.asks[0].quantity == Decimal("120")


async def test_synchronizer_primes_the_store_from_snapshots() -> None:
    store = BookStore(FreshnessPolicy(max_age_ms=10_000))
    adapters = {
        Venue.KALSHI: FixtureAdapter(Venue.KALSHI, FIXTURES),
        Venue.POLYMARKET: FixtureAdapter(Venue.POLYMARKET, FIXTURES),
    }
    sync = BookSynchronizer(store, adapters)
    await sync.prime({Venue.KALSHI: [KALSHI_FED], Venue.POLYMARKET: [POLY_FED]})

    assert len(store) == 2
    book, verdict = store.evaluate(Venue.KALSHI, KALSHI_FED)
    assert book is not None and verdict is not None
    assert verdict.is_fresh


async def test_synchronizer_marks_books_stale_when_priming_fails() -> None:
    """A market with no fixture must not silently leave a phantom fresh book."""
    store = BookStore(FreshnessPolicy(max_age_ms=10_000))
    adapters = {Venue.KALSHI: FixtureAdapter(Venue.KALSHI, FIXTURES)}
    sync = BookSynchronizer(store, adapters)
    await sync.prime({Venue.KALSHI: ["DOES-NOT-EXIST"]})
    assert store.get(Venue.KALSHI, "DOES-NOT-EXIST") is None


async def test_health_report_reflects_adapter_activity() -> None:
    store = BookStore()
    adapters: dict[Venue, FixtureAdapter] = {Venue.KALSHI: FixtureAdapter(Venue.KALSHI, FIXTURES)}
    sync = BookSynchronizer(store, dict(adapters))
    await sync.prime({Venue.KALSHI: [KALSHI_FED]})

    report = sync.health_report()
    assert report[0]["venue"] == Venue.KALSHI.value
    assert report[0]["messages_received"] >= 1
    assert report[0]["fresh"] is True
