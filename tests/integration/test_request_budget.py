"""How many venue requests one pair actually costs, counted rather than asserted.

The operations budget was prose, and the prose was wrong: it doubled the refresh cost by
assuming two requests per venue per cycle. That matters in both directions. Overstating it
makes an operator ask for rate-limit headroom they do not need and cannot justify to the
venue; understating it walks a live trial into a 429 mid-attempt. Kalshi's Developer
Agreement ties API use to facilitating one's own trading, so "roughly this much" is not a
good enough account of what we are spending.

So the budget is measured at the adapter boundary, and these numbers are what the runbook
and docs/operations.md quote. A change in request shape breaks this file rather than
quietly invalidating a document nobody re-derives.

Counted here for the **US** target — Kalshi + Polymarket US — because that is what the
trial runs. Polymarket *Global* costs two requests per book rather than one, and that
difference is pinned too so the legacy path cannot be mistaken for the trial's cost.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    DataSource,
    IngestionMode,
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import Venue, VenueProduct
from event_arb.domain.models import OrderBook
from event_arb.scanner import Scanner
from event_arb.venues.base import VenueAdapter
from event_arb.venues.fixture import FixtureAdapter

pytestmark = pytest.mark.anyio

KALSHI_FED = "KXFEDDECISION-26SEP-CUT25"
US_FED = "fed-cuts-25bps-september-2026"


def us_settings(fixture_dir: Path) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )


def count_book_fetches(scanner: Scanner) -> Counter[Venue]:
    """Count every ``fetch_orderbook`` the scanner makes, per venue.

    Counted at the adapter method rather than at the HTTP client because that is the unit
    the scanner controls. The per-request multiplier below converts it to HTTP calls.
    """
    counts: Counter[Venue] = Counter()

    class Counting:
        def __init__(self, venue: Venue, inner: Callable[[str], Awaitable[OrderBook]]) -> None:
            self._venue = venue
            self._inner = inner

        async def __call__(self, market_id: str) -> OrderBook:
            counts[self._venue] += 1
            return await self._inner(market_id)

    for venue, adapter in scanner.adapters.items():
        adapter.fetch_orderbook = Counting(venue, adapter.fetch_orderbook)  # type: ignore[method-assign]
    return counts


async def prepared(fixture_dir: Path) -> Scanner:
    settings = us_settings(fixture_dir)
    scanner = Scanner(settings)
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books({Venue.KALSHI: [KALSHI_FED], Venue.POLYMARKET: [US_FED]})
    return scanner


async def test_one_refresh_cycle_costs_one_book_fetch_per_venue(fixture_dir: Path) -> None:
    """The correction: one pair is one request per venue per cycle, not two."""
    scanner = await prepared(fixture_dir)
    counts = count_book_fetches(scanner)

    outcome = await scanner.refresh_books({Venue.KALSHI: [KALSHI_FED], Venue.POLYMARKET: [US_FED]})

    assert counts[Venue.KALSHI] == 1
    assert counts[Venue.POLYMARKET] == 1
    assert outcome.requests == 2, "and the outcome reports what it actually spent"
    assert outcome.is_complete


async def test_the_hourly_refresh_budget_follows_from_the_cadence(fixture_dir: Path) -> None:
    """720 per venue per hour at a 5 s cadence — an upper bound, not an estimate.

    Upper, because the cycle sleeps 5 s *between* cycles and the batch itself takes time
    on top, so a real hour fits fewer than 720 cycles rather than more.
    """
    per_cycle_per_venue = 1
    cadence_seconds = 5
    cycles_per_hour = 3600 // cadence_seconds

    assert cycles_per_hour == 720
    assert cycles_per_hour * per_cycle_per_venue == 720
    # The figure the old budget quoted was double this, from assuming two requests per
    # venue per cycle. One pair on the US target costs one.
    assert cycles_per_hour * per_cycle_per_venue * 2 == 1440


async def live_shaped(fixture_dir: Path) -> Scanner:
    """Live *settings* over fixture *adapters*.

    The refetch only happens when the data source is not fixtures — replaying a file twice
    is not a re-observation, and grading it as one would manufacture hedge evidence. So
    counting the live cost needs the live configuration, while the transport stays offline:
    no test in this suite may contact a venue.
    """
    settings = us_settings(fixture_dir).model_copy(
        update={
            "data_source": DataSource.LIVE,
            "ingestion_mode": IngestionMode.REST_POLLING,
        }
    )
    adapters: dict[Venue, VenueAdapter] = {
        Venue.KALSHI: FixtureAdapter(Venue.KALSHI, fixture_dir),
        Venue.POLYMARKET: FixtureAdapter(
            Venue.POLYMARKET, fixture_dir, product=VenueProduct.POLYMARKET_US
        ),
    }
    scanner = Scanner(settings, adapters=adapters)
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books({Venue.KALSHI: [KALSHI_FED], Venue.POLYMARKET: [US_FED]})
    return scanner


async def test_a_paper_trade_adds_one_refetch_per_venue(fixture_dir: Path) -> None:
    """REST polling has nothing updating the store between legs, so it goes back."""
    scanner = await live_shaped(fixture_dir)
    opportunity = scanner.scan().all_detected[0]
    counts = count_book_fetches(scanner)

    await scanner.paper_trade(opportunity)

    assert counts[Venue.KALSHI] == 1, "leg 1 re-reads its book at execution time"
    assert counts[Venue.POLYMARKET] == 1, "leg 2 re-reads after the latency gap"
    assert sum(counts.values()) == 2, "two extra requests per simulated trade, total"


async def test_a_fixture_run_never_refetches(fixture_dir: Path) -> None:
    """Replaying a file twice is not a re-observation, and must not cost or claim one."""
    scanner = await prepared(fixture_dir)
    opportunity = scanner.scan().all_detected[0]
    counts = count_book_fetches(scanner)

    await scanner.paper_trade(opportunity)

    assert sum(counts.values()) == 0


async def test_streaming_ingestion_does_not_pay_the_refetch(fixture_dir: Path) -> None:
    """A continuously updated store makes the re-read free — and genuine."""
    settings = us_settings(fixture_dir).model_copy(
        update={
            "data_source": DataSource.LIVE,
            "ingestion_mode": IngestionMode.WEBSOCKET,
        }
    )
    adapters: dict[Venue, VenueAdapter] = {
        Venue.KALSHI: FixtureAdapter(Venue.KALSHI, fixture_dir),
        Venue.POLYMARKET: FixtureAdapter(
            Venue.POLYMARKET, fixture_dir, product=VenueProduct.POLYMARKET_US
        ),
    }
    scanner = Scanner(settings, adapters=adapters)
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books({Venue.KALSHI: [KALSHI_FED], Venue.POLYMARKET: [US_FED]})
    opportunity = scanner.scan().all_detected[0]
    counts = count_book_fetches(scanner)

    await scanner.paper_trade(opportunity)

    assert sum(counts.values()) == 0


async def test_polymarket_global_costs_two_http_calls_per_book(fixture_dir: Path) -> None:
    """Pinned so the legacy path's cost cannot be mistaken for the trial's.

    Global has a separate book per outcome token, so one logical book is two HTTP GETs.
    The US product serves both sides in one response.
    """
    import inspect

    from event_arb.venues.polymarket.client import PolymarketClient
    from event_arb.venues.polymarket_us.client import PolymarketUSClient

    global_source = inspect.getsource(PolymarketClient.fetch_orderbook)
    us_source = inspect.getsource(PolymarketUSClient.fetch_orderbook)

    assert global_source.count("await self._get(") == 2
    assert us_source.count("await self._get(") == 1
