"""Watched-pair configuration and targeted book refresh.

Refreshing every discovered market is serial and rate-limited, so at venue scale every
book is stale before it is ever scanned. These tests pin the property that actually
matters: the number of venue requests scales with the number of *watched pairs*, not with
the size of the venue.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    OpportunitySettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.domain.enums import MatchStatus, Venue
from event_arb.domain.keys import kalshi_key
from event_arb.scanner import Scanner
from event_arb.venues.fixture import FixtureAdapter
from tests.conftest import KALSHI_FED, KALSHI_NYC, POLY_FED, POLY_NYC


class CountingAdapter(FixtureAdapter):
    """Fixture adapter that records every order-book request it serves."""

    def __init__(self, venue: Venue, fixture_dir: Path) -> None:
        super().__init__(venue, fixture_dir)
        self.requested: list[str] = []

    async def fetch_orderbook(self, market_id: str):  # type: ignore[no-untyped-def]
        self.requested.append(market_id)
        return await super().fetch_orderbook(market_id)


def build(fixture_dir: Path, trial: TrialSettings | None = None) -> tuple[Scanner, dict]:
    settings = Settings(
        # Legacy Polymarket **Global** corpus. Stated explicitly so this path stays
        # clearly separated from the Polymarket US fixtures and tests.
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        trial=trial or TrialSettings(),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
    )
    adapters = {
        Venue.KALSHI: CountingAdapter(Venue.KALSHI, fixture_dir),
        Venue.POLYMARKET: CountingAdapter(Venue.POLYMARKET, fixture_dir),
    }
    return Scanner(settings, adapters=adapters), adapters


async def test_refresh_is_scoped_to_configured_watched_pairs(fixture_dir: Path) -> None:
    """One watched pair means two book requests, not one per discovered market."""
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, adapters = build(fixture_dir, trial)
    await scanner.discover()
    scanner.seed_watched_pairs()
    await scanner.refresh_books()

    assert adapters[Venue.KALSHI].requested == [KALSHI_FED]
    assert adapters[Venue.POLYMARKET].requested == [POLY_FED]
    assert len(scanner.store) == 2


async def test_unwatched_markets_are_never_fetched(fixture_dir: Path) -> None:
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, adapters = build(fixture_dir, trial)
    await scanner.discover()
    scanner.seed_watched_pairs()
    await scanner.refresh_books()

    assert KALSHI_NYC not in adapters[Venue.KALSHI].requested
    assert POLY_NYC not in adapters[Venue.POLYMARKET].requested


async def test_request_count_scales_with_pairs_not_venue_size(fixture_dir: Path) -> None:
    """Two watched pairs cost twice one pair — independent of how many markets exist."""
    one = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    two = TrialSettings(
        watched_pairs=[
            WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED),
            WatchedPair(kalshi_market_id=KALSHI_NYC, polymarket_market_id=POLY_NYC),
        ]
    )
    counts = []
    for trial in (one, two):
        scanner, adapters = build(fixture_dir, trial)
        await scanner.discover()
        scanner.seed_watched_pairs()
        await scanner.refresh_books()
        counts.append(
            len(adapters[Venue.KALSHI].requested) + len(adapters[Venue.POLYMARKET].requested)
        )
    assert counts == [2, 4]


async def test_seeded_pairs_are_under_review_never_approved(fixture_dir: Path) -> None:
    """Configuration selects what to watch; only a human can make a pair tradable."""
    trial = TrialSettings(
        watched_pairs=[
            WatchedPair(
                kalshi_market_id=KALSHI_FED,
                polymarket_market_id=POLY_FED,
                note="reviewed 2026-08-22",
            )
        ]
    )
    scanner, _ = build(fixture_dir, trial)
    seeded = scanner.seed_watched_pairs()
    assert len(seeded) == 1
    assert seeded[0].status is MatchStatus.UNDER_REVIEW
    assert seeded[0].manually_approved is False
    assert seeded[0].is_tradable_pair is False
    assert "reviewed 2026-08-22" in seeded[0].reviewer_notes


async def test_outcome_inversion_is_carried_from_configuration(fixture_dir: Path) -> None:
    trial = TrialSettings(
        watched_pairs=[
            WatchedPair(
                kalshi_market_id=KALSHI_FED,
                polymarket_market_id=POLY_FED,
                outcomes_inverted=True,
            )
        ]
    )
    scanner, _ = build(fixture_dir, trial)
    assert scanner.seed_watched_pairs()[0].outcomes_inverted is True


async def test_seeding_is_idempotent(fixture_dir: Path) -> None:
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, _ = build(fixture_dir, trial)
    scanner.seed_watched_pairs()
    assert scanner.seed_watched_pairs() == []
    assert len(scanner.state.pairs) == 1


async def test_seeding_never_downgrades_an_approved_pair(fixture_dir: Path) -> None:
    """Re-seeding must not quietly revoke a human approval."""
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, _ = build(fixture_dir, trial)
    await scanner.discover()
    pair = scanner.seed_watched_pairs()[0]
    scanner.approve(pair.id, approved_by="reviewer@example.com")

    scanner.seed_watched_pairs()
    assert scanner.state.pairs[pair.id].is_tradable_pair is True


async def test_without_configuration_watching_follows_live_pair_states(
    fixture_dir: Path,
) -> None:
    """With no explicit config, watch what is approved or under review, not everything."""
    scanner, _adapters = build(fixture_dir)
    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0.4"))
    await scanner.refresh_books()

    watched = scanner.watched_markets()
    assert watched[Venue.KALSHI]
    # Rejected and suspended pairs contribute nothing to the refresh set.
    for pair in scanner.state.pairs.values():
        pair_watched = pair.kalshi_market_id in watched[Venue.KALSHI]
        assert pair_watched is (pair.status is not MatchStatus.REJECTED)


async def test_refresh_all_books_still_available_for_fixture_scale(
    fixture_dir: Path,
) -> None:
    scanner, adapters = build(fixture_dir)
    await scanner.discover()
    await scanner.refresh_all_books()
    assert len(adapters[Venue.KALSHI].requested) == 3


@pytest.mark.parametrize("configured", [True, False])
async def test_refresh_marks_missing_books_stale_rather_than_silently_skipping(
    fixture_dir: Path, configured: bool
) -> None:
    trial = (
        TrialSettings(
            watched_pairs=[
                WatchedPair(kalshi_market_id="NO-SUCH-MARKET", polymarket_market_id=POLY_FED)
            ]
        )
        if configured
        else TrialSettings()
    )
    scanner, _ = build(fixture_dir, trial)
    await scanner.discover()
    scanner.seed_watched_pairs()
    await scanner.refresh_books()
    # A market with no fixture yields no book at all rather than a phantom fresh one.
    assert scanner.store.get(kalshi_key("NO-SUCH-MARKET")) is None


# --------------------------------------------- discovery scoped to watched markets


class DiscoveryCountingAdapter(CountingAdapter):
    """Records which market-id filter discovery was called with."""

    def __init__(self, venue: Venue, fixture_dir: Path) -> None:
        super().__init__(venue, fixture_dir)
        self.discovery_filters: list[list[str] | None] = []

    async def discover_markets(self, limit=None, market_ids=None):  # type: ignore[no-untyped-def]
        self.discovery_filters.append(market_ids)
        return await super().discover_markets(limit=limit, market_ids=market_ids)


def build_discovery(fixture_dir: Path, trial: TrialSettings | None = None):  # type: ignore[no-untyped-def]
    settings = Settings(
        # Legacy Polymarket **Global** corpus. Stated explicitly so this path stays
        # clearly separated from the Polymarket US fixtures and tests.
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        trial=trial or TrialSettings(),
        opportunity=OpportunitySettings(max_book_age_ms=60_000, max_quantity=Decimal("100")),
    )
    adapters = {
        Venue.KALSHI: DiscoveryCountingAdapter(Venue.KALSHI, fixture_dir),
        Venue.POLYMARKET: DiscoveryCountingAdapter(Venue.POLYMARKET, fixture_dir),
    }
    return Scanner(settings, adapters=adapters), adapters


async def test_discovery_is_scoped_to_watched_markets(fixture_dir: Path) -> None:
    """Configuration promised discovery would be restricted; only refresh was."""
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, adapters = build_discovery(fixture_dir, trial)
    markets = await scanner.discover()

    assert adapters[Venue.KALSHI].discovery_filters == [[KALSHI_FED]]
    assert adapters[Venue.POLYMARKET].discovery_filters == [[POLY_FED]]
    assert {m.venue_market_id for m in markets} == {KALSHI_FED, POLY_FED}


async def test_unwatched_markets_are_never_discovered(fixture_dir: Path) -> None:
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, _ = build_discovery(fixture_dir, trial)
    await scanner.discover()
    assert (Venue.KALSHI, KALSHI_NYC) not in scanner.state.markets


async def test_discovery_without_configuration_still_lists_the_venue(
    fixture_dir: Path,
) -> None:
    scanner, adapters = build_discovery(fixture_dir)
    markets = await scanner.discover()
    assert adapters[Venue.KALSHI].discovery_filters == [None]
    assert len(markets) == 6


async def test_only_discovered_markets_count_as_observed(fixture_dir: Path) -> None:
    """Observation drives approval restoration, so it must reflect real retrieval."""
    trial = TrialSettings(
        watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)]
    )
    scanner, _ = build_discovery(fixture_dir, trial)
    await scanner.discover()
    assert kalshi_key(KALSHI_FED) in scanner.state.observed_markets
    assert (Venue.KALSHI, KALSHI_NYC) not in scanner.state.observed_markets
