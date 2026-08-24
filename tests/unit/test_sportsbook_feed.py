"""Fixture-backed odds feeds and the shared provider mapper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.sportsbook.base import ProviderDataError, QuoteFreshnessPolicy
from event_arb.sportsbook.enums import (
    DataProvider,
    MarketType,
    QuoteStatus,
    SportsbookRiskFlag,
)
from event_arb.sportsbook.fixture import FixtureOddsFeed
from event_arb.sportsbook.providers import opticodds

FIXTURES = Path(__file__).parent.parent / "fixtures" / "sportsbook"
PROVIDERS = [DataProvider.OPTICODDS, DataProvider.ODDSJAM]


def feed(provider: DataProvider) -> FixtureOddsFeed:
    return FixtureOddsFeed(provider, FIXTURES)


# ------------------------------------------------------------------- discovery


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_both_provider_identities_share_one_schema(provider: DataProvider) -> None:
    """developer.oddsjam.com redirects to OpticOdds: one API, two provenance labels."""
    events = await feed(provider).discover_events()
    assert len(events) == 2
    assert all(e.provider is provider for e in events)


async def test_home_and_away_orientation_is_explicit() -> None:
    """An inverted spread is a bet on the other team, so orientation is recorded."""
    events = await feed(DataProvider.OPTICODDS).discover_events()
    nba = next(e for e in events if e.league == "NBA")
    assert nba.home is not None and nba.home.name == "Boston Celtics"
    assert nba.away is not None and nba.away.name == "Los Angeles Lakers"


async def test_discovery_filters_by_sport_and_league() -> None:
    assert len(await feed(DataProvider.OPTICODDS).discover_events(league="NBA")) == 1
    assert len(await feed(DataProvider.OPTICODDS).discover_events(sport="Football")) == 1


# ---------------------------------------------------------------------- quotes


async def test_quotes_parse_with_exact_decimals_and_no_floats() -> None:
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes(["EVT-NBA-0001"])
    total = next(q for q in quotes if q.market_type is MarketType.TOTAL)
    assert total.line == Decimal("220.5")
    assert isinstance(total.line, Decimal)
    assert isinstance(total.odds, Decimal)
    assert total.odds == Decimal("-108")


async def test_missing_limit_is_none_not_a_default() -> None:
    """`limits.max: null` means no published limit. It does not mean unlimited."""
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes(["EVT-NBA-0001"])
    fanduel = next(q for q in quotes if q.sportsbook == "FanDuel")
    assert fanduel.max_stake is None
    assert fanduel.has_published_limit is False
    assert SportsbookRiskFlag.LIQUIDITY_UNVERIFIED in fanduel.risk_flags()


async def test_published_limit_is_carried_through() -> None:
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes(["EVT-NBA-0001"])
    dk = next(
        q for q in quotes if q.sportsbook == "DraftKings" and q.market_type is MarketType.MONEYLINE
    )
    assert dk.max_stake == Decimal("2500")
    assert SportsbookRiskFlag.LIQUIDITY_UNVERIFIED not in dk.risk_flags()


async def test_every_quote_flags_manual_execution_and_book_discretion() -> None:
    """Nothing here can place a bet, and the book can always refuse."""
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes([])
    for quote in quotes:
        flags = quote.risk_flags()
        assert SportsbookRiskFlag.MANUAL_EXECUTION_REQUIRED in flags
        assert SportsbookRiskFlag.BOOK_MAY_REFUSE_OR_LIMIT in flags


async def test_whole_number_line_flags_push_risk() -> None:
    """A -3 spread can land exactly on 3; a -3.5 cannot."""
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes(["EVT-NBA-0001"])
    spread = next(q for q in quotes if q.market_type is MarketType.SPREAD)
    assert spread.line == Decimal("-3")
    assert SportsbookRiskFlag.PUSH_POSSIBLE in spread.risk_flags()


async def test_quotes_filter_by_sportsbook() -> None:
    quotes = await feed(DataProvider.OPTICODDS).fetch_quotes(
        ["EVT-NBA-0001"], sportsbooks=["FanDuel"]
    )
    assert {q.sportsbook for q in quotes} == {"FanDuel"}


# ---------------------------------------------------------------------- stream


async def test_stream_skips_control_frames_and_emits_quotes() -> None:
    updates = [u async for u in feed(DataProvider.OPTICODDS).stream(["basketball"])]
    assert updates
    assert all(u.entry_id for u in updates)


async def test_locked_odds_becomes_a_suspended_quote() -> None:
    """locked-odds covers suspension and removal without distinguishing them."""
    updates = [u async for u in feed(DataProvider.OPTICODDS).stream(["basketball"])]
    suspended = [u for u in updates if u.is_suspension]
    assert suspended
    assert suspended[0].quote.status is QuoteStatus.SUSPENDED
    assert suspended[0].quote.is_actionable is False


async def test_line_movement_is_visible_across_frames() -> None:
    updates = [u async for u in feed(DataProvider.OPTICODDS).stream(["basketball"])]
    spreads = [u.quote for u in updates if u.quote.market_type is MarketType.SPREAD]
    assert spreads and spreads[-1].line == Decimal("-3.5")


async def test_duplicate_updates_share_a_quote_key() -> None:
    """Deduplication is by identity, not by payload equality."""
    updates = [u async for u in feed(DataProvider.OPTICODDS).stream(["basketball"])]
    keys = [u.quote.quote_key for u in updates]
    assert len(keys) != len(set(keys)), "fixture should contain a repeated quote"


async def test_malformed_frame_is_counted_not_swallowed() -> None:
    """A frame we cannot parse means we are missing an update the book already made."""
    adapter = feed(DataProvider.OPTICODDS)
    [u async for u in adapter.stream(["basketball"])]
    assert adapter.health.malformed_messages == 1
    assert adapter.health.last_error is not None


async def test_stream_tracks_continuity_token() -> None:
    adapter = feed(DataProvider.OPTICODDS)
    [u async for u in adapter.stream(["basketball"])]
    assert adapter.health.last_entry_id is not None


def test_health_report_omits_the_continuity_token() -> None:
    """entry_id is provider state, not an operator metric, and reports get shared."""
    adapter = feed(DataProvider.OPTICODDS)
    adapter.health.last_entry_id = "1730000000-9"
    assert "last_entry_id" not in adapter.health.as_dict()


# --------------------------------------------------------------- malformed input


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "not-a-list"},
        {"data": [{"id": "E1", "odds": "not-a-list"}]},
        {"data": [{"odds": []}]},
        {"data": [{"id": "E1", "odds": [{"sportsbook": "DK"}]}]},
        {"data": [{"id": "E1", "odds": [{"price": -110}]}]},
    ],
)
def test_malformed_payloads_raise_rather_than_half_parse(payload: dict[str, object]) -> None:
    with pytest.raises(ProviderDataError):
        opticodds.parse_quotes(payload, DataProvider.OPTICODDS)


def test_float_price_is_refused_at_the_mapper() -> None:
    """Defence against a caller using response.json() instead of parse_float=Decimal."""
    payload = {
        "data": [
            {
                "id": "E1",
                "odds": [
                    {"sportsbook": "DK", "selection": "X", "market_id": "moneyline", "price": 2.5}
                ],
            }
        ]
    }
    with pytest.raises(ProviderDataError, match="parse_float=Decimal"):
        opticodds.parse_quotes(payload, DataProvider.OPTICODDS)


def test_unknown_market_id_maps_to_other_not_a_guess() -> None:
    payload = {
        "data": [
            {
                "id": "E1",
                "odds": [
                    {
                        "sportsbook": "DK",
                        "selection": "X",
                        "market_id": "some_exotic_prop",
                        "price": -110,
                    }
                ],
            }
        ]
    }
    quote = opticodds.parse_quotes(payload, DataProvider.OPTICODDS)[0]
    assert quote.market_type is MarketType.OTHER


# ------------------------------------------------------------------- freshness


def test_freshness_judged_from_local_receipt_not_provider_clock() -> None:
    """A provider clock running fast must not make an old price look actionable."""
    payload = {
        "data": [
            {
                "id": "E1",
                "odds": [
                    {
                        "sportsbook": "DK",
                        "selection": "X",
                        "market_id": "moneyline",
                        "price": -110,
                        "timestamp": 99999999999,
                    }
                ],
            }
        ]
    }
    quote = opticodds.parse_quotes(payload, DataProvider.OPTICODDS)[0]
    old = quote.model_copy(update={"received_at": datetime.now(UTC) - timedelta(minutes=5)})
    assert QuoteFreshnessPolicy(max_age_ms=30_000).is_fresh(old) is False
    assert QuoteFreshnessPolicy(max_age_ms=30_000).apply(old).is_stale is True


def test_fresh_quote_passes_through_unchanged() -> None:
    payload = {
        "data": [
            {
                "id": "E1",
                "odds": [
                    {"sportsbook": "DK", "selection": "X", "market_id": "moneyline", "price": -110}
                ],
            }
        ]
    }
    quote = opticodds.parse_quotes(payload, DataProvider.OPTICODDS)[0]
    assert QuoteFreshnessPolicy().apply(quote) is quote


def test_no_bet_placement_surface_exists() -> None:
    """Structural guarantee across the whole package."""
    forbidden = {"place_bet", "submit_bet", "bet", "wager", "submit", "place_order"}
    assert not (forbidden & set(dir(FixtureOddsFeed)))
    assert not (forbidden & set(dir(opticodds)))
