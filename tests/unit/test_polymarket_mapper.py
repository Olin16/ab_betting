"""Polymarket payload normalization."""

from __future__ import annotations

from decimal import Decimal

import pytest

from event_arb.domain.enums import SettlementAsset, Venue
from event_arb.venues.base import MarketDataError
from event_arb.venues.polymarket.mapper import build_book, parse_market

YES = {
    "bids": [{"price": "0.6100", "size": "400"}, {"price": "0.6200", "size": "200"}],
    "asks": [{"price": "0.6500", "size": "500"}, {"price": "0.6400", "size": "250"}],
    "timestamp": "1789000000000",
}
NO = {
    "bids": [{"price": "0.3600", "size": "250"}],
    "asks": [{"price": "0.3800", "size": "200"}, {"price": "0.3900", "size": "150"}],
}


def test_bids_descending_asks_ascending() -> None:
    book = build_book("M", YES, NO)
    assert [level.price for level in book.yes.bids] == [Decimal("0.62"), Decimal("0.61")]
    assert [level.price for level in book.yes.asks] == [Decimal("0.64"), Decimal("0.65")]


def test_settlement_asset_is_usdc_not_usd() -> None:
    """Polymarket settles in USDC on Polygon. Conflating it with USD hides basis risk."""
    market = parse_market(
        {
            "condition_id": "0xabc",
            "question": "Q",
            "category": "finance",
            "tokens": [{"token_id": "1", "outcome": "YES"}, {"token_id": "2", "outcome": "NO"}],
        }
    )
    assert market.settlement_asset is SettlementAsset.USDC
    assert market.venue is Venue.POLYMARKET


def test_no_side_derived_by_reciprocity_when_absent() -> None:
    book = build_book("M", YES, None)
    # YES ask 0.64 -> NO bid 0.36; YES bid 0.62 -> NO ask 0.38
    assert book.no.bids[0].price == Decimal("0.36")
    assert book.no.asks[0].price == Decimal("0.38")


def test_real_no_book_preferred_over_derived() -> None:
    book = build_book("M", YES, NO)
    assert book.no.asks[0].quantity == Decimal("200")


def test_polymarket_books_carry_no_sequence_number() -> None:
    """The market channel exposes only a hash; this must stay visible, not assumed away."""
    assert build_book("M", YES, NO).sequence is None


def test_zero_size_levels_dropped() -> None:
    book = build_book("M", {"bids": [{"price": "0.5", "size": "0"}], "asks": []}, None)
    assert book.yes.bids == ()


@pytest.mark.parametrize(
    "payload",
    [{"bids": "nope"}, {"bids": [{"price": "0.5"}]}, {"bids": [{"price": "x", "size": "1"}]}],
)
def test_malformed_raises(payload: dict[str, object]) -> None:
    with pytest.raises(MarketDataError):
        build_book("M", payload, None)


def test_market_missing_id_raises() -> None:
    with pytest.raises(MarketDataError):
        parse_market({"question": "no id"})


def test_closed_market_not_tradable() -> None:
    market = parse_market({"condition_id": "0x1", "question": "Q", "closed": True})
    assert market.is_tradable is False


def test_unknown_category_uses_conservative_highest_fee() -> None:
    """Guessing low on fees would invent profit; guessing high only suppresses it."""
    market = parse_market({"condition_id": "0x1", "question": "Q", "category": "unheard-of"})
    assert market.fee_config.taker_rate == Decimal("0.07")
    assert "conservative" in market.fee_config.source


def test_known_category_uses_published_rate() -> None:
    market = parse_market({"condition_id": "0x1", "question": "Q", "category": "finance"})
    assert market.fee_config.taker_rate == Decimal("0.04")
