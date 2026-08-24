"""Schema-contract tests against payloads recorded from the live public APIs.

Recorded 2026-08-23 from unauthenticated endpoints. These exist to catch the failure that
synthetic fixtures cannot: our *assumptions* drifting from the venues' actual responses.
Several assumptions were wrong when these were first captured, and each is pinned below.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from event_arb.domain.enums import SettlementAsset
from event_arb.venues.kalshi.mapper import parse_market as parse_kalshi
from event_arb.venues.kalshi.mapper import parse_orderbook
from event_arb.venues.polymarket_us.mapper import (
    RulesCompleteness,
    rules_completeness,
)
from event_arb.venues.polymarket_us.mapper import (
    parse_market as parse_us,
)

RECORDED = Path(__file__).parent.parent / "fixtures" / "recorded"


def load(name: str) -> dict[str, Any]:
    data = json.loads((RECORDED / name).read_text(), parse_float=Decimal)
    assert isinstance(data, dict)
    return data


def kalshi_market() -> dict[str, Any]:
    return load("kalshi_markets.json")["markets"][0]


def us_market() -> dict[str, Any]:
    return load("polymarket_us_markets.json")["markets"][0]


# ------------------------------------------------------------------------ Kalshi


def test_kalshi_market_parses_from_a_real_payload() -> None:
    market = parse_kalshi(kalshi_market())
    assert market.venue_market_id
    assert market.settlement_asset is SettlementAsset.USD


def test_kalshi_carries_no_tick_size_field() -> None:
    """The assumption that `tick_size_dollars` exists was wrong."""
    raw = kalshi_market()
    assert "tick_size_dollars" not in raw
    assert "tick_size" not in raw


def test_kalshi_tick_comes_from_price_ranges_step() -> None:
    """Real markets are deci-cent (0.001). Defaulting to a penny skipped valid levels."""
    raw = kalshi_market()
    assert raw["price_level_structure"] == "deci_cent"
    assert parse_kalshi(raw).tick_size == Decimal("0.0010")


def test_kalshi_carries_no_settlement_source_field() -> None:
    """It stays empty rather than being invented, and that gap is material at approval."""
    raw = kalshi_market()
    assert "settlement_source" not in raw
    assert parse_kalshi(raw).settlement_source == ""


def test_kalshi_dollar_and_fp_fields_are_strings() -> None:
    """Assumption A2: prices are dollar strings, quantities fixed-point strings."""
    raw = kalshi_market()
    assert isinstance(raw["yes_bid_dollars"], str)
    assert isinstance(raw["volume_fp"], str)


def test_kalshi_orderbook_shape_matches_assumption_a2() -> None:
    """orderbook_fp with yes_dollars / no_dollars, bids only."""
    book = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert book.is_complete is False


# ------------------------------------------------------------------ Polymarket US


def test_us_market_parses_from_a_real_payload() -> None:
    market = parse_us(us_market())
    assert market.venue_market_id
    assert market.settlement_asset is SettlementAsset.USD


def test_us_fee_coefficient_is_published_per_market() -> None:
    """feeCoefficient satisfies the per-market fee requirement directly."""
    raw = us_market()
    assert "feeCoefficient" in raw
    market = parse_us(raw)
    assert market.fee_config.taker_rate == Decimal(str(raw["feeCoefficient"]))
    assert market.fee_config.source == "market.feeCoefficient"


def test_us_outcomes_arrive_as_a_json_encoded_string() -> None:
    """`outcomes` is '["Yes","No"]'. Iterating it undecoded would yield characters."""
    raw = us_market()
    assert isinstance(raw["outcomes"], str)
    assert parse_us(raw).outcomes == ("Yes", "No")


def test_us_market_sides_carry_the_authoritative_identity() -> None:
    sides = us_market()["marketSides"]
    assert isinstance(sides, list) and sides
    assert {"identifier", "description", "long"} <= set(sides[0])


def test_us_tick_is_finer_than_a_penny() -> None:
    assert parse_us(us_market()).tick_size == Decimal("0.001")


def test_us_status_is_an_enum_string() -> None:
    raw = us_market()
    assert raw["status"].startswith("MARKET_STATUS_")
    assert parse_us(raw).is_active is True


def test_us_quotes_are_denominated_in_usd() -> None:
    """bestBidQuote/bestAskQuote name their currency explicitly."""
    raw = us_market()
    assert raw["bestBidQuote"]["currency"] == "USD"


def test_us_carries_no_resolution_source_field() -> None:
    """No named settlement source is published; it must not be fabricated."""
    raw = us_market()
    assert "resolutionSource" not in raw
    assert parse_us(raw).settlement_source == ""


def test_us_description_does_carry_enforceable_rules() -> None:
    """The open question from the plan, now answered against a real payload.

    Real descriptions state postponement, rescheduling, cancellation and elimination
    behaviour — substantive settlement conditions, not a marketing blurb.
    """
    description = us_market()["description"]
    assert rules_completeness(description) is RulesCompleteness.LIKELY_COMPLETE
    lowered = description.lower()
    assert "postponed" in lowered
    assert "settle" in lowered


def test_us_market_with_real_rules_is_tradable() -> None:
    assert parse_us(us_market()).is_tradable is True


def test_no_global_concepts_appear_in_us_payloads() -> None:
    """Confirms the products really are separate at the wire level."""
    raw = json.dumps(us_market(), default=str).lower()
    for concept in ("condition_id", "token_id", "polygon", "usdc"):
        assert concept not in raw
