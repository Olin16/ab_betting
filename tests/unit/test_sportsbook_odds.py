"""Exact odds arithmetic and format-conversion boundaries."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from event_arb.sportsbook.enums import OddsFormat
from event_arb.sportsbook.odds import (
    OddsConversionError,
    american_to_decimal,
    decimal_to_american,
    fractional_to_decimal,
    implied_probability,
    parse_odds,
    profit_on_stake,
    total_return,
)


@pytest.mark.parametrize(
    ("american", "expected"),
    [
        ("100", "2"),  # even money
        ("-100", "2"),  # even money from the other sign
        ("150", "2.5"),
        ("-150", "1.666667"),
        ("-110", "1.909091"),  # the standard vig price
        ("200", "3"),
        ("-200", "1.5"),
        ("2500", "26"),
        ("-10000", "1.01"),
    ],
)
def test_american_to_decimal(american: str, expected: str) -> None:
    assert american_to_decimal(Decimal(american)) == Decimal(expected)


def test_positive_and_negative_american_meet_at_even_money() -> None:
    """+100 and -100 are the same price; the sign convention flips there."""
    assert american_to_decimal(Decimal("100")) == american_to_decimal(Decimal("-100"))


@pytest.mark.parametrize("bad", ["0", "50", "-50", "99", "-99"])
def test_american_odds_inside_the_dead_band_are_rejected(bad: str) -> None:
    """No price can promise less than even money on both sides of the sign."""
    with pytest.raises(OddsConversionError):
        american_to_decimal(Decimal(bad))


@pytest.mark.parametrize(
    ("decimal_odds", "expected"),
    [("2", "100"), ("2.5", "150"), ("1.5", "-200"), ("3", "200"), ("1.909091", "-109.999989")],
)
def test_decimal_to_american(decimal_odds: str, expected: str) -> None:
    assert decimal_to_american(Decimal(decimal_odds)) == Decimal(expected)


@pytest.mark.parametrize("bad", ["1", "0.5", "-2", "0"])
def test_decimal_odds_at_or_below_one_are_rejected(bad: str) -> None:
    with pytest.raises(OddsConversionError):
        decimal_to_american(Decimal(bad))


@pytest.mark.parametrize(
    ("frac", "expected"), [("5/2", "3.5"), ("1/1", "2"), ("1/2", "1.5"), ("10/11", "1.909091")]
)
def test_fractional_to_decimal(frac: str, expected: str) -> None:
    assert parse_odds(frac, OddsFormat.FRACTIONAL) == Decimal(expected)


@pytest.mark.parametrize("bad", ["5", "5/0", "-5/2", "a/b", "5/-2"])
def test_malformed_fractional_odds_rejected(bad: str) -> None:
    with pytest.raises(OddsConversionError):
        parse_odds(bad, OddsFormat.FRACTIONAL)


def test_probability_format_round_trips() -> None:
    assert parse_odds("0.4", OddsFormat.PROBABILITY) == Decimal("2.5")


@pytest.mark.parametrize("bad", ["0", "1", "1.5", "-0.2"])
def test_probability_outside_the_open_unit_interval_rejected(bad: str) -> None:
    with pytest.raises(OddsConversionError):
        parse_odds(bad, OddsFormat.PROBABILITY)


def test_floats_are_refused_outright() -> None:
    """A float here means precision was already lost by the JSON decoder."""
    with pytest.raises(OddsConversionError, match="parse_float=Decimal"):
        parse_odds(2.5, OddsFormat.DECIMAL)  # type: ignore[arg-type]


def test_payout_math_is_exact() -> None:
    """100 at +150 returns 250 and profits 150 — exactly, not 249.99999."""
    odds = american_to_decimal(Decimal("150"))
    assert total_return(Decimal("100"), odds) == Decimal("250.0000")
    assert profit_on_stake(Decimal("100"), odds) == Decimal("150.0000")


def test_implied_probability_includes_the_vig() -> None:
    """Two -110 sides sum to more than 1; the excess is the book's margin."""
    side = implied_probability(american_to_decimal(Decimal("-110")))
    assert side * 2 > Decimal("1")


def test_fractional_helper_rejects_bad_inputs() -> None:
    with pytest.raises(OddsConversionError):
        fractional_to_decimal(Decimal("1"), Decimal("0"))


@given(
    american=st.integers(min_value=100, max_value=100_000).map(Decimal)
    # -100 is deliberately excluded: it is the same price as +100, and the round trip
    # canonicalizes even money to the positive form. See the even-money test above.
    | st.integers(min_value=-100_000, max_value=-101).map(Decimal)
)
def test_american_decimal_round_trip_is_stable(american: Decimal) -> None:
    """Round-tripping is accurate to within 0.1% relative.

    Not exact, and that is a property of the representation rather than a defect:
    heavily-favoured prices compress into a narrow band just above decimal odds 1.0, so
    quantizing to six places costs proportionally more the further from even money you go.
    A relative bound states that honestly; an absolute one would either be vacuous at
    -100 or unsatisfiable at -100000.
    """
    decimal_odds = american_to_decimal(american)
    assert decimal_odds > Decimal("1")
    round_tripped = decimal_to_american(decimal_odds)
    relative_error = abs(round_tripped - american) / abs(american)
    assert relative_error < Decimal("0.001")


@pytest.mark.parametrize(
    ("american", "exact"),
    [
        ("-10000", True),  # -> 1.01 exactly, lands on the 6dp grid
        ("-200", True),  # -> 1.5 exactly
        ("150", True),  # -> 2.5 exactly
        ("-110", False),  # -> 1.909090909..., does not
        ("-2599", False),  # -> 1.038476..., does not
    ],
)
def test_round_trip_is_exact_only_when_decimal_odds_land_on_the_grid(
    american: str, exact: bool
) -> None:
    """Documents *why* the bound above is relative rather than zero.

    Error is not a function of magnitude; it is a function of whether ``1 + 100/N`` is
    representable in six places. -10000 round-trips perfectly while -110 does not.
    """
    value = Decimal(american)
    error = abs(decimal_to_american(american_to_decimal(value)) - value)
    assert (error == 0) is exact
    assert error / abs(value) < Decimal("0.001")


def test_even_money_round_trip_canonicalizes_to_the_positive_form() -> None:
    """-100 and +100 are one price; the round trip picks +100 and that is not a bug."""
    assert decimal_to_american(american_to_decimal(Decimal("-100"))) == Decimal("100.000000")


@given(
    stake=st.decimals(min_value=Decimal("1"), max_value=Decimal("10000"), places=2),
    american=st.integers(min_value=100, max_value=5000).map(Decimal),
)
def test_return_always_exceeds_stake_on_a_winner(stake: Decimal, american: Decimal) -> None:
    odds = american_to_decimal(american)
    assert total_return(stake, odds) > stake
    assert profit_on_stake(stake, odds) > 0
