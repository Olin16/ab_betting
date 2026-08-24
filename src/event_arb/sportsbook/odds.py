"""Exact odds arithmetic.

Every conversion here is closed over :class:`~decimal.Decimal`. Odds are the input to a
payout calculation, and a payout calculation that drifts is a payout calculation that
invents or destroys edge, so binary floats are excluded at the boundary rather than
tolerated and rounded later.

The formats:

============ ======================================== =================================
Format       Meaning                                  To decimal odds
============ ======================================== =================================
American +N  Profit on a 100 stake                    ``1 + N/100``
American -N  Stake needed to win 100                  ``1 + 100/N``
Decimal D    Total return per 1 staked (stake incl.)  ``D``
Fractional   ``a/b`` profit per ``b`` staked          ``1 + a/b``
Probability  Implied probability ``p``                ``1/p``
============ ======================================== =================================

American odds have no representation between -100 and +100 exclusive: a price cannot
promise less than even money on both sides of the sign. Values in that band are rejected
rather than coerced.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from event_arb.domain.money import ZERO, quantize_price
from event_arb.sportsbook.enums import OddsFormat

__all__ = [
    "ODDS_EXP",
    "american_to_decimal",
    "decimal_to_american",
    "fractional_to_decimal",
    "implied_probability",
    "parse_odds",
    "profit_on_stake",
    "to_decimal_odds",
    "total_return",
]

#: Decimal odds are carried to 6 places: enough that a 1-cent payout difference on a
#: realistic stake never rounds away, without pretending to precision the book does not use.
ODDS_EXP = Decimal("0.000001")

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


class OddsConversionError(ValueError):
    """Raised when a value cannot be interpreted as odds in the stated format."""


def _q(value: Decimal) -> Decimal:
    return value.quantize(ODDS_EXP)


def american_to_decimal(american: Decimal) -> Decimal:
    """Convert American odds to decimal odds.

    ``+150`` -> ``2.5``   (win 150 on a 100 stake, total return 250)
    ``-200`` -> ``1.5``   (stake 200 to win 100, total return 300)
    """
    if american == ZERO:
        raise OddsConversionError("American odds of 0 are not meaningful")
    if -_HUNDRED < american < _HUNDRED:
        raise OddsConversionError(f"American odds must be <= -100 or >= +100; got {american}")
    if american > ZERO:
        return _q(_ONE + american / _HUNDRED)
    return _q(_ONE + _HUNDRED / -american)


def decimal_to_american(decimal_odds: Decimal) -> Decimal:
    """Convert decimal odds back to American odds.

    Decimal odds of exactly 2.0 are even money and map to ``+100`` by convention.
    """
    if decimal_odds <= _ONE:
        raise OddsConversionError(f"decimal odds must be > 1; got {decimal_odds}")
    if decimal_odds >= Decimal("2"):
        return _q((decimal_odds - _ONE) * _HUNDRED)
    return _q(-_HUNDRED / (decimal_odds - _ONE))


def fractional_to_decimal(numerator: Decimal, denominator: Decimal) -> Decimal:
    """``a/b`` means profit ``a`` per ``b`` staked, so decimal odds are ``1 + a/b``."""
    if denominator <= ZERO:
        raise OddsConversionError("fractional denominator must be positive")
    if numerator <= ZERO:
        raise OddsConversionError("fractional numerator must be positive")
    return _q(_ONE + numerator / denominator)


def parse_odds(value: str | Decimal | float, odds_format: OddsFormat) -> Decimal:
    """Parse a raw odds value in ``odds_format`` into **decimal** odds.

    Strings are parsed directly into ``Decimal``; a ``float`` input is rejected, because by
    the time a float exists the precision question has already been decided badly.
    """
    # float is in the signature only so this guard can reject it. Provider payloads are
    # untyped JSON, and a float arriving here means the precision question was already
    # decided badly upstream — by a decoder that did not use parse_float=Decimal.
    if isinstance(value, float):
        raise OddsConversionError(
            "refusing to read odds from a float; decode provider JSON with "
            "json.loads(..., parse_float=Decimal)"
        )

    if odds_format is OddsFormat.FRACTIONAL:
        text = str(value).strip()
        if "/" not in text:
            raise OddsConversionError(f"fractional odds must look like 'a/b'; got {text!r}")
        left, _, right = text.partition("/")
        try:
            return fractional_to_decimal(Decimal(left.strip()), Decimal(right.strip()))
        except InvalidOperation as exc:
            raise OddsConversionError(f"unparseable fractional odds {text!r}") from exc

    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise OddsConversionError(f"unparseable odds {value!r}") from exc

    if odds_format is OddsFormat.AMERICAN:
        return american_to_decimal(number)
    if odds_format is OddsFormat.DECIMAL:
        if number <= _ONE:
            raise OddsConversionError(f"decimal odds must be > 1; got {number}")
        return _q(number)
    if odds_format is OddsFormat.PROBABILITY:
        if not (ZERO < number < _ONE):
            raise OddsConversionError(f"probability must be strictly between 0 and 1; got {number}")
        return _q(_ONE / number)
    raise OddsConversionError(f"unsupported odds format {odds_format}")


def to_decimal_odds(value: str | Decimal | float, odds_format: OddsFormat) -> Decimal:
    """Alias for :func:`parse_odds`, named for the call sites that read better this way."""
    return parse_odds(value, odds_format)


def implied_probability(decimal_odds: Decimal) -> Decimal:
    """Implied probability, **including the book's margin**.

    Summed across a market's selections this exceeds 1; the excess is the vig. It is a
    useful diagnostic but is never the basis of a profit calculation here — payouts are.
    """
    if decimal_odds <= _ONE:
        raise OddsConversionError(f"decimal odds must be > 1; got {decimal_odds}")
    return _q(_ONE / decimal_odds)


def total_return(stake: Decimal, decimal_odds: Decimal) -> Decimal:
    """Total returned on a winning bet, stake included."""
    if stake < ZERO:
        raise OddsConversionError("stake cannot be negative")
    return quantize_price(stake * decimal_odds)


def profit_on_stake(stake: Decimal, decimal_odds: Decimal) -> Decimal:
    """Profit on a winning bet, stake excluded."""
    return quantize_price(total_return(stake, decimal_odds) - stake)
