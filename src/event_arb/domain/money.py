"""Fixed-point money and price primitives.

Every monetary value in this project is a :class:`decimal.Decimal`. Binary floats are
never used for money, prices, or quantities: ``0.1 + 0.2 != 0.3`` in binary floating
point, and a fee model that rounds at the fourth decimal place cannot tolerate that.

Three quantization grids are used:

``PRICE_EXP``  (1e-4)
    Contract prices in dollars. Kalshi publishes ``"0.4200"`` style dollar strings and
    supports sub-penny pricing; Polymarket prices are decimal strings on a tick grid.

``QTY_EXP``  (1e-2)
    Contract quantities. Kalshi's fixed-point counts (``"13.00"``) permit fractional
    contracts.

``CENTICENT_EXP`` (1e-4) / ``CENT_EXP`` (1e-2)
    Balance precision grids used by the Kalshi fee-rounding model.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

__all__ = [
    "CENTICENT_EXP",
    "CENT_EXP",
    "ONE_DOLLAR",
    "PRICE_EXP",
    "QTY_EXP",
    "ZERO",
    "ceil_to",
    "dec",
    "floor_to",
    "is_valid_price",
    "quantize_price",
    "quantize_qty",
    "round_half_even_to",
]

#: Working precision. Generous enough that intermediate products never lose digits.
_PRECISION: Final = 34

PRICE_EXP: Final[Decimal] = Decimal("0.0001")
QTY_EXP: Final[Decimal] = Decimal("0.01")
CENTICENT_EXP: Final[Decimal] = Decimal("0.0001")
CENT_EXP: Final[Decimal] = Decimal("0.01")

ZERO: Final[Decimal] = Decimal("0")
ONE_DOLLAR: Final[Decimal] = Decimal("1.00")


def dec(value: str | int | Decimal) -> Decimal:
    """Coerce to :class:`Decimal` without ever passing through ``float``.

    ``float`` is rejected rather than silently converted: accepting it would let a
    binary-float rounding error enter the money path at the one point this module exists
    to protect.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; almost certainly a bug.
        raise TypeError("bool is not a valid monetary value")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"refusing to build Decimal from {type(value).__name__}; use str or int")


def _q(value: Decimal, exp: Decimal, rounding: str) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return value.quantize(exp, rounding=rounding)


def quantize_price(value: Decimal) -> Decimal:
    """Snap a price to the 1e-4 dollar grid (half-even)."""
    return _q(value, PRICE_EXP, ROUND_HALF_EVEN)


def quantize_qty(value: Decimal) -> Decimal:
    """Snap a contract quantity to the 1e-2 grid (half-even)."""
    return _q(value, QTY_EXP, ROUND_HALF_EVEN)


def ceil_to(value: Decimal, exp: Decimal) -> Decimal:
    """Round *up* (toward +inf) to the given exponent grid.

    Used for fees, which venues round in their own favour.
    """
    return _q(value, exp, ROUND_CEILING)


def floor_to(value: Decimal, exp: Decimal) -> Decimal:
    """Round *down* (toward -inf) to the given exponent grid.

    Used for balance changes and for executable quantities, where rounding up would
    overstate what we can actually fill.
    """
    return _q(value, exp, ROUND_FLOOR)


def round_half_even_to(value: Decimal, exp: Decimal) -> Decimal:
    """Banker's rounding to the given grid."""
    return _q(value, exp, ROUND_HALF_EVEN)


def is_valid_price(value: Decimal) -> bool:
    """A tradable binary-contract price lies strictly between $0 and $1."""
    return ZERO < value < ONE_DOLLAR
