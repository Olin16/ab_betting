"""Money primitives: the foundation everything else's correctness rests on."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from event_arb.domain.money import (
    CENT_EXP,
    CENTICENT_EXP,
    ceil_to,
    dec,
    floor_to,
    is_valid_price,
    quantize_price,
    quantize_qty,
)


def test_dec_rejects_float() -> None:
    """A float in the money path is a bug, not a convenience."""
    with pytest.raises(TypeError):
        dec(0.1)  # type: ignore[arg-type]


def test_dec_rejects_bool() -> None:
    with pytest.raises(TypeError):
        dec(True)  # type: ignore[arg-type]


def test_dec_accepts_str_and_int() -> None:
    assert dec("0.07") == Decimal("0.07")
    assert dec(3) == Decimal("3")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.00001", "0.0001"),
        ("0.0001", "0.0001"),
        ("0.00010001", "0.0002"),
        ("0", "0.0000"),
    ],
)
def test_ceil_to_centicent(value: str, expected: str) -> None:
    assert ceil_to(Decimal(value), CENTICENT_EXP) == Decimal(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("-0.1541", "-0.16"), ("-0.15", "-0.15"), ("0.199", "0.19")],
)
def test_floor_to_cent_goes_toward_negative_infinity(value: str, expected: str) -> None:
    """Flooring must be toward -inf, not toward zero: Kalshi's balance rounding depends on it."""
    assert floor_to(Decimal(value), CENT_EXP) == Decimal(expected)


def test_quantize_grids() -> None:
    assert quantize_price(Decimal("0.123456")) == Decimal("0.1235")
    assert quantize_qty(Decimal("13.004")) == Decimal("13.00")


@pytest.mark.parametrize(
    ("price", "valid"),
    [("0.0001", True), ("0.9999", True), ("0", False), ("1.00", False), ("1.5", False)],
)
def test_is_valid_price(price: str, valid: bool) -> None:
    assert is_valid_price(Decimal(price)) is valid


@given(st.decimals(min_value=Decimal("0"), max_value=Decimal("1000"), places=6))
def test_ceil_never_below_and_floor_never_above(value: Decimal) -> None:
    """Rounding direction is a safety property, not a formatting preference."""
    assert ceil_to(value, CENTICENT_EXP) >= value
    assert floor_to(value, CENTICENT_EXP) <= value
