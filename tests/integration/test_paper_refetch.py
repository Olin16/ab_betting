"""REST paper trading must go back to the venue at each leg's arrival.

Re-reading the cached snapshot cost nothing and always returned the detection-time book,
so leg 2 could not fail and the hedge rate was structurally 100%.

Every scanner here is built with **injected fixture adapters**, so nothing in this module
can reach a venue even though it configures REST-polling live mode.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

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
from event_arb.domain.models import Opportunity, OrderBook
from event_arb.paper.evidence import EvidenceGrade
from event_arb.scanner import Scanner
from event_arb.venues.base import VenueAdapter
from event_arb.venues.fixture import FixtureAdapter

pytestmark = pytest.mark.anyio


def _build(fixture_dir: Path, *, live_rest: bool) -> tuple[Scanner, list[tuple[Venue, str]]]:
    settings = Settings(
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
    if live_rest:
        # Only the flags that select the re-fetch path. Adapters stay on fixtures below,
        # so no live client is ever constructed.
        settings = settings.model_copy(
            update={
                "data_source": DataSource.LIVE,
                "ingestion_mode": IngestionMode.REST_POLLING,
            }
        )
    calls: list[tuple[Venue, str]] = []
    adapters: dict[Venue, VenueAdapter] = {}
    for venue in (Venue.KALSHI, Venue.POLYMARKET):
        adapter = FixtureAdapter(venue, fixture_dir, product=VenueProduct.POLYMARKET_US)
        inner = adapter.fetch_orderbook

        async def counted(market_id: str, *, _v: Venue = venue, _inner: Any = inner) -> OrderBook:
            calls.append((_v, market_id))
            return await _inner(market_id)

        adapter.fetch_orderbook = counted  # type: ignore[method-assign]
        adapters[venue] = adapter
    return Scanner(settings, adapters=adapters), calls


async def _first_opportunity(scanner: Scanner) -> Opportunity:
    await scanner.discover()
    await scanner.refresh_all_books()
    scanner.generate_pairs()
    result = scanner.scan()
    assert result.opportunities, "fixture corpus should produce at least one opportunity"
    return result.opportunities[0]


async def test_fixture_mode_does_not_refetch_and_admits_the_evidence_is_weak(
    fixture_dir: Path,
) -> None:
    """A fixture file cannot move, so a hedge success from it is not a measurement."""
    scanner, calls = _build(fixture_dir, live_rest=False)
    opportunity = await _first_opportunity(scanner)

    before = len(calls)
    trade = await scanner.paper_trade(opportunity, simulate_latency=False)

    assert len(calls) == before, "fixture mode must not issue extra venue requests"
    assert trade.leg2_evidence is EvidenceGrade.UNCHANGED_REREAD
    assert trade.leg2_evidence.supports_hedge_claims is False
    assert any("does not demonstrate behaviour under adverse movement" in n for n in trade.notes)


async def test_rest_polling_refetches_each_leg_and_the_requests_are_countable(
    fixture_dir: Path,
) -> None:
    """Two extra venue requests per simulated trade — declared, not hidden."""
    scanner, calls = _build(fixture_dir, live_rest=True)
    opportunity = await _first_opportunity(scanner)

    before = len(calls)
    trade = await scanner.paper_trade(opportunity, simulate_latency=False)

    assert len(calls) - before == 2, "each leg must re-read the venue at arrival"
    assert calls[-2][0] is Venue.KALSHI
    assert calls[-1][0] is Venue.POLYMARKET
    # The fixture returns the same book, and the grade says so rather than claiming a
    # measurement. A real venue returning a moved book is what upgrades this to measured.
    assert trade.leg2_evidence is EvidenceGrade.UNCHANGED_REREAD


async def test_a_moved_leg2_book_is_what_earns_the_measured_grade(fixture_dir: Path) -> None:
    """Only a re-fetch that comes back different can support a hedge-rate claim."""
    scanner, _ = _build(fixture_dir, live_rest=True)
    opportunity = await _first_opportunity(scanner)

    polymarket = scanner.adapters[Venue.POLYMARKET]
    original = polymarket.fetch_orderbook

    async def thinner(market_id: str) -> OrderBook:
        book = await original(market_id)
        # Halve the depth at every ask level: the book moved against us while leg 1 filled.
        return book.model_copy(
            update={
                "no": book.no.model_copy(
                    update={
                        "asks": tuple(
                            level.model_copy(update={"quantity": level.quantity / 2})
                            for level in book.no.asks
                        )
                    }
                ),
                "yes": book.yes.model_copy(
                    update={
                        "asks": tuple(
                            level.model_copy(update={"quantity": level.quantity / 2})
                            for level in book.yes.asks
                        )
                    }
                ),
            }
        )

    polymarket.fetch_orderbook = thinner  # type: ignore[method-assign]
    trade = await scanner.paper_trade(opportunity, simulate_latency=False)

    assert trade.leg2_evidence is EvidenceGrade.MEASURED
    assert trade.leg2_evidence.supports_hedge_claims is True


async def test_a_failed_refetch_is_a_real_leg2_failure_not_a_stale_fill(
    fixture_dir: Path,
) -> None:
    """Falling back to the cached book would record a clean fill that never happened."""
    scanner, _ = _build(fixture_dir, live_rest=True)
    opportunity = await _first_opportunity(scanner)

    async def broken(market_id: str) -> OrderBook:
        raise TimeoutError("venue unreachable")

    scanner.adapters[Venue.POLYMARKET].fetch_orderbook = broken  # type: ignore[method-assign]
    trade = await scanner.paper_trade(opportunity, simulate_latency=False)

    assert trade.unhedged_quantity > 0, "leg 1 exposure must be reported as unhedged"
    assert any("leg 2 failed" in note for note in trade.notes)
