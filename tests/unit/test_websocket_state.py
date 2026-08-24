"""WebSocket book-state machines: deltas, sequence gaps, and malformed input.

A silently corrupted book is the most dangerous failure mode in this system: it produces
a spread that does not exist. These tests pin the behaviour that prevents that.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal

import pytest

from event_arb.config import KalshiSettings
from event_arb.domain.enums import Outcome, Venue
from event_arb.venues.base import AdapterHealth, BackoffPolicy
from event_arb.venues.kalshi.websocket import KalshiBookState, KalshiWebSocketStream
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


# ------------------------------------------- per-market subscriptions keyed by sid


class FakeConnection:
    """Captures what the stream sends, so subscription shape can be asserted."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def make_stream() -> KalshiWebSocketStream:
    return KalshiWebSocketStream(
        settings=KalshiSettings(), health=AdapterHealth(venue=Venue.KALSHI)
    )


async def test_each_market_gets_its_own_subscription_command() -> None:
    """One subscription per market makes sid unambiguous.

    A single subscription naming several markets leaves it undefined whether seq advances
    per market or across the subscription; tracking per market under a shared sid would
    manufacture a gap on every interleaved update.
    """
    stream = make_stream()
    payloads = [json.loads(stream._subscribe_payload(market)) for market in ("MKT-A", "MKT-B")]
    assert [p["params"]["market_tickers"] for p in payloads] == [["MKT-A"], ["MKT-B"]]
    assert payloads[0]["id"] != payloads[1]["id"]


async def test_subscription_pins_the_price_convention() -> None:
    """Kalshi has signalled the default may change; relying on it would flip NO prices."""
    payload = json.loads(make_stream()._subscribe_payload("MKT-A"))
    assert payload["params"]["use_yes_price"] is True


async def test_sid_is_bound_to_the_market_that_requested_it() -> None:
    stream = make_stream()
    payload = json.loads(stream._subscribe_payload("MKT-A"))
    stream._bind_subscription({"type": "subscribed", "id": payload["id"], "sid": 7})
    assert stream._sids["MKT-A"] == 7
    assert stream._states[7].market_id == "MKT-A"


async def test_interleaved_markets_do_not_manufacture_sequence_gaps() -> None:
    """Two markets advancing their own counters must not look like gaps to each other."""
    stream = make_stream()
    connection = FakeConnection()
    for market, sid in (("MKT-A", 1), ("MKT-B", 2)):
        payload = json.loads(stream._subscribe_payload(market))
        stream._bind_subscription({"type": "subscribed", "id": payload["id"], "sid": sid})

    snapshot = {"yes_dollars_fp": [["0.5000", "100.00"]], "no_dollars_fp": []}
    for sid in (1, 2):
        await stream._handle(
            {"type": "orderbook_snapshot", "sid": sid, "seq": 1, "msg": snapshot},
            connection,  # type: ignore[arg-type]
            [],
        )

    # Interleave deltas; each subscription's seq advances independently.
    for sid, seq in ((1, 2), (2, 2), (1, 3), (2, 3)):
        event = await stream._handle(
            {
                "type": "orderbook_delta",
                "sid": sid,
                "seq": seq,
                "msg": {"price_dollars": "0.5000", "delta_fp": "1.00", "side": "yes"},
            },
            connection,  # type: ignore[arg-type]
            [],
        )
        assert event is not None
        assert event.book.is_stale is False

    assert stream._health.sequence_gaps == 0
    assert not [m for m in connection.sent if m.get("cmd") == "update_subscription"]


async def test_a_real_gap_on_one_subscription_resyncs_only_that_market() -> None:
    stream = make_stream()
    connection = FakeConnection()
    for market, sid in (("MKT-A", 1), ("MKT-B", 2)):
        payload = json.loads(stream._subscribe_payload(market))
        stream._bind_subscription({"type": "subscribed", "id": payload["id"], "sid": sid})

    snapshot = {"yes_dollars_fp": [["0.5000", "100.00"]], "no_dollars_fp": []}
    for sid in (1, 2):
        await stream._handle(
            {"type": "orderbook_snapshot", "sid": sid, "seq": 1, "msg": snapshot},
            connection,  # type: ignore[arg-type]
            [],
        )

    # Skip seq 2 on subscription 1 only.
    event = await stream._handle(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 5,
            "msg": {"price_dollars": "0.5000", "delta_fp": "1.00", "side": "yes"},
        },
        connection,  # type: ignore[arg-type]
        [],
    )
    assert event is not None
    assert event.book.is_stale is True
    assert stream._health.sequence_gaps == 1

    resyncs = [m for m in connection.sent if m.get("cmd") == "update_subscription"]
    assert len(resyncs) == 1
    assert resyncs[0]["params"]["market_tickers"] == ["MKT-A"]
    assert resyncs[0]["params"]["sid"] == 1
    # The healthy subscription is untouched.
    assert stream._states[2].needs_resync is False


async def test_unbound_sid_falls_back_to_the_ticker_on_the_message() -> None:
    """A missed confirmation must not silently drop a market's whole feed."""
    stream = make_stream()
    event = await stream._handle(
        {
            "type": "orderbook_snapshot",
            "sid": 99,
            "seq": 1,
            "msg": {
                "market_ticker": "MKT-C",
                "yes_dollars_fp": [["0.5000", "100.00"]],
                "no_dollars_fp": [],
            },
        },
        FakeConnection(),  # type: ignore[arg-type]
        [],
    )
    assert event is not None
    assert event.market_id == "MKT-C"


# ------------------------------------- malformed updates must stale the book at once


async def test_malformed_delta_emits_a_stale_book_not_silence() -> None:
    """A delta we could not apply leaves the book missing a change the venue has made.

    Returning None left the previous book eligible for opportunity detection until its
    freshness timer happened to expire — the same corruption a sequence gap causes, but
    without the stale marker that suppresses it.
    """
    stream = make_stream()
    connection = FakeConnection()
    payload = json.loads(stream._subscribe_payload("MKT-A"))
    stream._bind_subscription({"type": "subscribed", "id": payload["id"], "sid": 1})
    await stream._handle(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"yes_dollars_fp": [["0.5000", "100.00"]], "no_dollars_fp": []},
        },
        connection,  # type: ignore[arg-type]
        [],
    )

    event = await stream._handle(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {"price_dollars": "not-a-price", "delta_fp": "1.00", "side": "yes"},
        },
        connection,  # type: ignore[arg-type]
        [],
    )
    assert event is not None, "malformed delta must still emit an event"
    assert event.book.is_stale is True
    assert stream._states[1].needs_resync is True
    assert [m for m in connection.sent if m.get("cmd") == "update_subscription"]


def test_global_polymarket_stream_refuses_us_settings() -> None:
    from event_arb.config import PolymarketSettings
    from event_arb.venues.polymarket.websocket import PolymarketWebSocketStream

    with pytest.raises(ValueError, match="US requires its own authenticated WebSocket"):
        PolymarketWebSocketStream(
            settings=PolymarketSettings(),
            health=AdapterHealth(venue=Venue.POLYMARKET),
            token_map={},
        )


async def test_polymarket_malformed_price_change_stales_the_book() -> None:
    from event_arb.config import PolymarketProduct, PolymarketSettings
    from event_arb.venues.polymarket.websocket import PolymarketWebSocketStream

    stream = PolymarketWebSocketStream(
        settings=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        health=AdapterHealth(venue=Venue.POLYMARKET),
        token_map={"MKT": {"YES": "tok-yes", "NO": "tok-no"}},
    )
    stream._handle(
        {
            "event_type": "book",
            "asset_id": "tok-yes",
            "bids": [{"price": "0.61", "size": "400"}],
            "asks": [{"price": "0.64", "size": "250"}],
        }
    )
    event = stream._handle(
        {
            "event_type": "price_change",
            "price_changes": [{"asset_id": "tok-yes", "price": "bad", "size": "1", "side": "BUY"}],
        }
    )
    assert event is not None
    assert event.book.is_stale is True
