"""Kalshi payload normalization, especially the bids-only -> two-sided conversion."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.domain.enums import Outcome, SettlementAsset, Venue
from event_arb.venues.base import MarketDataError
from event_arb.venues.kalshi.mapper import parse_market, parse_orderbook, parse_ts

PAYLOAD = {
    "orderbook_fp": {
        "yes_dollars": [["0.5000", "150.00"], ["0.5300", "120.00"]],
        "no_dollars": [["0.4300", "180.00"], ["0.4500", "300.00"]],
    }
}


def test_parses_dollar_strings_as_decimal_not_float() -> None:
    book = parse_orderbook("M", PAYLOAD)
    assert book.yes.bids[0].price == Decimal("0.53")
    assert isinstance(book.yes.bids[0].price, Decimal)
    assert book.yes.bids[0].quantity == Decimal("120.00")


def test_bids_sorted_best_first_despite_ascending_input() -> None:
    """Kalshi sends ascending (best bid last); the domain model is best-first."""
    book = parse_orderbook("M", PAYLOAD)
    assert [level.price for level in book.yes.bids] == [Decimal("0.53"), Decimal("0.50")]
    assert [level.price for level in book.no.bids] == [Decimal("0.45"), Decimal("0.43")]


def test_asks_are_synthesized_from_the_opposite_side() -> None:
    """A NO bid at $0.45 is a YES ask at $0.55, same quantity. This is an identity."""
    book = parse_orderbook("M", PAYLOAD)
    assert book.yes.asks[0].price == Decimal("0.55")
    assert book.yes.asks[0].quantity == Decimal("300.00")
    assert book.no.asks[0].price == Decimal("0.47")
    assert book.no.asks[0].quantity == Decimal("120.00")


def test_yes_ask_and_no_bid_always_sum_to_one() -> None:
    book = parse_orderbook("M", PAYLOAD)
    for ask, bid in zip(book.yes.asks, book.no.bids, strict=True):
        assert ask.price + bid.price == Decimal("1")


def test_accepts_websocket_snapshot_field_names() -> None:
    """WS snapshots use *_dollars_fp; REST uses *_dollars. Both must parse."""
    book = parse_orderbook(
        "M",
        {"yes_dollars_fp": [["0.0800", "300.00"]], "no_dollars_fp": [["0.5400", "20.00"]]},
    )
    assert book.yes.bids[0].price == Decimal("0.08")
    assert book.no.bids[0].quantity == Decimal("20.00")


def test_zero_quantity_levels_are_dropped() -> None:
    book = parse_orderbook(
        "M",
        {"orderbook_fp": {"yes_dollars": [["0.5000", "0.00"]], "no_dollars": [["0.4000", "5.00"]]}},
    )
    assert book.yes.bids == ()


def test_empty_book_is_marked_incomplete() -> None:
    book = parse_orderbook("M", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert book.is_complete is False


@pytest.mark.parametrize(
    "payload",
    [
        {"orderbook_fp": {"yes_dollars": "not-a-list"}},
        {"orderbook_fp": {"yes_dollars": [["0.50"]]}},
        {"orderbook_fp": {"yes_dollars": [["abc", "1.00"]]}},
    ],
)
def test_malformed_payloads_raise_marketdataerror(payload: dict[str, object]) -> None:
    """A schema change must fail loudly, never produce a half-built book."""
    with pytest.raises(MarketDataError):
        parse_orderbook("M", payload)


def test_missing_orderbook_key_raises() -> None:
    with pytest.raises(MarketDataError):
        parse_orderbook("M", {"something_else": {}})


def test_parse_market_normalizes_metadata() -> None:
    market = parse_market(
        {
            "ticker": "KX-T",
            "title": "Test market",
            "status": "active",
            "close_time": "2026-09-17T18:00:00Z",
            "rules_primary": "Primary rules.",
            "rules_secondary": "Secondary rules.",
            "tick_size_dollars": "0.01",
        }
    )
    assert market.venue is Venue.KALSHI
    assert market.settlement_asset is SettlementAsset.USD
    assert market.is_tradable is True
    assert market.rules_text == "Primary rules. Secondary rules."
    assert market.close_time == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def test_parse_market_requires_ticker() -> None:
    with pytest.raises(MarketDataError):
        parse_market({"title": "no ticker"})


def test_inactive_market_is_not_tradable() -> None:
    market = parse_market({"ticker": "X", "status": "settled"})
    assert market.is_tradable is False


def test_parse_ts_handles_iso_and_epoch_ms() -> None:
    assert parse_ts("2026-09-17T18:00:00Z") == datetime(2026, 9, 17, 18, 0, tzinfo=UTC)
    assert parse_ts(1669149841000) == datetime.fromtimestamp(1669149841, tz=UTC)
    assert parse_ts(None) is None
    assert parse_ts("garbage") is None


def test_outcome_opposite() -> None:
    assert Outcome.YES.opposite is Outcome.NO
    assert Outcome.NO.opposite is Outcome.YES
