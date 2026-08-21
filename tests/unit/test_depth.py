"""Depth walking: executable quantity and VWAP, never midpoints."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from event_arb.domain.models import BookLevel
from event_arb.opportunities.depth import available_depth, walk_asks

ASKS = (
    BookLevel(price=Decimal("0.55"), quantity=Decimal("100")),
    BookLevel(price=Decimal("0.56"), quantity=Decimal("200")),
    BookLevel(price=Decimal("0.60"), quantity=Decimal("500")),
)


def test_fill_within_first_level() -> None:
    walk = walk_asks(ASKS, Decimal("50"))
    assert walk.filled_quantity == Decimal("50")
    assert walk.vwap == Decimal("0.55")
    assert walk.levels_consumed == 1
    assert walk.is_complete


def test_vwap_walks_multiple_levels() -> None:
    """100@0.55 + 100@0.56 = 111.00 for 200 -> VWAP 0.555, not the 0.55 top of book."""
    walk = walk_asks(ASKS, Decimal("200"))
    assert walk.gross_cost == Decimal("111.00")
    assert walk.vwap == Decimal("0.5550")
    assert walk.worst_price == Decimal("0.56")
    assert walk.levels_consumed == 2


def test_vwap_is_worse_than_top_of_book_for_large_size() -> None:
    """The whole point: size costs more than the best price advertises."""
    small = walk_asks(ASKS, Decimal("10")).vwap
    large = walk_asks(ASKS, Decimal("400")).vwap
    assert large > small


def test_partial_fill_reported_honestly() -> None:
    walk = walk_asks(ASKS, Decimal("10000"))
    assert walk.filled_quantity == Decimal("800")
    assert walk.is_complete is False


def test_price_ceiling_stops_the_walk() -> None:
    """A level above the ceiling would already have destroyed the edge."""
    walk = walk_asks(ASKS, Decimal("500"), max_price=Decimal("0.56"))
    assert walk.filled_quantity == Decimal("300")
    assert walk.worst_price == Decimal("0.56")


def test_empty_book_fills_nothing() -> None:
    walk = walk_asks((), Decimal("100"))
    assert walk.filled_quantity == Decimal("0")
    assert walk.vwap == Decimal("0")
    assert walk.worst_price == Decimal("0")


def test_zero_or_negative_request_fills_nothing() -> None:
    assert walk_asks(ASKS, Decimal("0")).filled_quantity == Decimal("0")
    assert walk_asks(ASKS, Decimal("-5")).filled_quantity == Decimal("0")


def test_off_tick_levels_are_skipped() -> None:
    """A price off the venue's grid means our book model disagrees with the venue."""
    asks = (
        BookLevel(price=Decimal("0.555"), quantity=Decimal("100")),
        BookLevel(price=Decimal("0.56"), quantity=Decimal("100")),
    )
    walk = walk_asks(asks, Decimal("100"), tick_size=Decimal("0.01"))
    assert walk.worst_price == Decimal("0.56")
    assert walk.filled_quantity == Decimal("100")


def test_quantity_increment_rounds_down_never_up() -> None:
    """Rounding size up would claim a fill the book cannot support."""
    walk = walk_asks(ASKS, Decimal("55"), min_quantity_increment=Decimal("10"))
    assert walk.filled_quantity == Decimal("50")


def test_available_depth_respects_price_limit() -> None:
    assert available_depth(ASKS) == Decimal("800")
    assert available_depth(ASKS, max_price=Decimal("0.56")) == Decimal("300")


@given(
    quantity=st.decimals(min_value=Decimal("1"), max_value=Decimal("800"), places=0),
)
def test_vwap_always_between_best_and_worst_price_touched(quantity: Decimal) -> None:
    walk = walk_asks(ASKS, quantity)
    if walk.filled_quantity > 0:
        assert ASKS[0].price <= walk.vwap <= walk.worst_price


@given(quantity=st.decimals(min_value=Decimal("1"), max_value=Decimal("2000"), places=0))
def test_gross_cost_never_exceeds_worst_price_times_quantity(quantity: Decimal) -> None:
    walk = walk_asks(ASKS, quantity)
    if walk.filled_quantity > 0:
        assert walk.gross_cost <= walk.worst_price * walk.filled_quantity


@given(quantity=st.decimals(min_value=Decimal("0"), max_value=Decimal("5000"), places=0))
def test_never_fills_more_than_requested_or_more_than_available(quantity: Decimal) -> None:
    walk = walk_asks(ASKS, quantity)
    assert walk.filled_quantity <= quantity
    assert walk.filled_quantity <= Decimal("800")
