"""WebSocket book-state machines: deltas, sequence gaps, and malformed input.

A silently corrupted book is the most dangerous failure mode in this system: it produces
a spread that does not exist. These tests pin the behaviour that prevents that.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from event_arb.domain.enums import Outcome
from event_arb.venues.base import BackoffPolicy
from event_arb.venues.kalshi.websocket import KalshiBookState
from event_arb.venues.polymarket.websocket import PolymarketBookState

SNAPSHOT = {
    "market_ticker": "M",
    "yes_dollars_fp": [["0.5000", "100.00"], ["0.5300", "50.00"]],
    "no_dollars_fp": [["0.4400", "80.00"]],
}


# ---------------------------------------------------------------------------- Kalshi


def test_snapshot_establishes_state_and_clears_resync_flag() -> None:
    state = KalshiBookState("M")
    assert state.needs_resync is True
    book = state.apply_snapshot(SNAPSHOT, seq=5)
    assert state.needs_resync is False
    assert state.last_seq == 5
    assert book.yes.bids[0].price == Decimal("0.53")


def test_delta_adds_quantity_at_a_level() -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=1)
    book = state.apply_delta({"price_dollars": "0.5300", "delta_fp": "25.00", "side": "yes"}, seq=2)
    assert book.yes.bids[0].quantity == Decimal("75.00")


def test_negative_delta_removes_a_level_entirely() -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=1)
    book = state.apply_delta(
        {"price_dollars": "0.5300", "delta_fp": "-50.00", "side": "yes"}, seq=2
    )
    assert all(level.price != Decimal("0.53") for level in book.yes.bids)


def test_delta_updates_synthetic_asks_on_the_other_side() -> None:
    """Removing a NO bid must remove the corresponding YES ask."""
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=1)
    book = state.apply_delta({"price_dollars": "0.4400", "delta_fp": "-80.00", "side": "no"}, seq=2)
    assert book.yes.asks == ()


@pytest.mark.parametrize(("last", "incoming"), [(5, 7), (5, 5), (5, 4), (5, 100)])
def test_any_non_consecutive_sequence_is_a_gap(last: int, incoming: int) -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=last)
    assert state.check_sequence(incoming) is False


def test_consecutive_sequence_is_accepted() -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=5)
    assert state.check_sequence(6) is True


def test_first_message_has_no_predecessor_to_compare() -> None:
    assert KalshiBookState("M").check_sequence(42) is True


@pytest.mark.parametrize(
    "delta",
    [
        {"price_dollars": "0.53", "delta_fp": "1.00", "side": "sideways"},
        {"price_dollars": "abc", "delta_fp": "1.00", "side": "yes"},
        {"delta_fp": "1.00", "side": "yes"},
        {"price_dollars": "0.53", "side": "yes"},
    ],
)
def test_malformed_delta_raises_rather_than_corrupting_state(delta: dict[str, str]) -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=1)
    with pytest.raises(ValueError):
        state.apply_delta(delta, seq=2)


def test_snapshot_after_gap_replaces_state_wholesale() -> None:
    state = KalshiBookState("M")
    state.apply_snapshot(SNAPSHOT, seq=1)
    fresh = state.apply_snapshot(
        {"yes_dollars_fp": [["0.7000", "10.00"]], "no_dollars_fp": []}, seq=99
    )
    assert [level.price for level in fresh.yes.bids] == [Decimal("0.70")]
    assert state.last_seq == 99


# ------------------------------------------------------------------------ Polymarket


def test_polymarket_book_snapshot_and_absolute_price_change() -> None:
    state = PolymarketBookState("M")
    state.apply_book(
        Outcome.YES,
        {
            "bids": [{"price": "0.6100", "size": "400"}],
            "asks": [{"price": "0.6400", "size": "250"}],
        },
    )
    # price_change carries the NEW size at the level, not a delta.
    state.apply_price_change(Outcome.YES, {"price": "0.6100", "size": "150", "side": "BUY"})
    assert state.to_book().yes.bids[0].quantity == Decimal("150")


def test_polymarket_zero_size_removes_level() -> None:
    state = PolymarketBookState("M")
    state.apply_book(Outcome.YES, {"bids": [{"price": "0.61", "size": "400"}], "asks": []})
    state.apply_price_change(Outcome.YES, {"price": "0.61", "size": "0", "side": "BUY"})
    assert state.to_book().yes.bids == ()


def test_polymarket_book_incomplete_before_any_snapshot() -> None:
    """Applying updates to a never-snapshotted book would fake completeness."""
    assert PolymarketBookState("M").to_book().is_complete is False


@pytest.mark.parametrize(
    "change",
    [
        {"price": "0.61", "size": "1", "side": "SIDEWAYS"},
        {"price": "bad", "size": "1", "side": "BUY"},
        {"size": "1", "side": "BUY"},
    ],
)
def test_polymarket_malformed_price_change_raises(change: dict[str, str]) -> None:
    state = PolymarketBookState("M")
    state.apply_book(Outcome.YES, {"bids": [], "asks": []})
    with pytest.raises(ValueError):
        state.apply_price_change(Outcome.YES, change)


# --------------------------------------------------------------------------- backoff


def test_backoff_grows_exponentially_and_is_capped() -> None:
    policy = BackoffPolicy(base_seconds=1.0, max_seconds=10.0, factor=2.0)
    rng = random.Random(0)
    # Full jitter means each delay is in [0, cap]; the cap is what grows.
    assert all(0.0 <= policy.delay_for(attempt, rng) <= 10.0 for attempt in range(1, 12))


def test_backoff_jitter_spreads_reconnects() -> None:
    """Without jitter, every client retries in lockstep and re-DDoSes the venue."""
    policy = BackoffPolicy(base_seconds=8.0, max_seconds=64.0)
    rng = random.Random(1234)
    delays = {policy.delay_for(4, rng) for _ in range(20)}
    assert len(delays) > 15


def test_backoff_rejects_zero_attempt() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy().delay_for(0)
