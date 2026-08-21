"""Risk flags attached to detected opportunities.

A flag is a *warning that travels with the opportunity*, not a rejection. Rejections
(:class:`~event_arb.domain.enums.RejectionReason`) mean "this is not an opportunity";
flags mean "this may be an opportunity, and here is what could still go wrong".

:attr:`~event_arb.domain.enums.RiskFlag.NON_ATOMIC_EXECUTION` is attached to every
opportunity without exception. There is no cross-venue atomic settlement between Kalshi
and Polymarket, so the first leg can always fill while the second fails. No opportunity
this system produces is risk-free, and the data model says so on every record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from event_arb.domain.enums import RiskFlag, SettlementAsset
from event_arb.domain.models import MarketPair, OrderBook, VenueMarket
from event_arb.orderbooks.freshness import FreshnessVerdict

__all__ = ["THIN_BOOK_LEVELS", "assess_risk_flags"]

#: A book with this many levels or fewer offers little protection against a single
#: cancellation removing the entire apparent edge.
THIN_BOOK_LEVELS = 2

#: Within this window before close, execution and settlement risk rise sharply.
NEAR_CLOSE_MINUTES = 30

#: A spread this wide suggests an illiquid or stale market rather than a real price.
WIDE_SPREAD = Decimal("0.10")


def _spread(book: OrderBook) -> Decimal | None:
    best_bid = book.yes.best_bid
    best_ask = book.yes.best_ask
    if best_bid is None or best_ask is None:
        return None
    return best_ask.price - best_bid.price


def assess_risk_flags(
    pair: MarketPair,
    kalshi_market: VenueMarket,
    polymarket_market: VenueMarket,
    kalshi_book: OrderBook,
    polymarket_book: OrderBook,
    kalshi_freshness: FreshnessVerdict | None = None,
    polymarket_freshness: FreshnessVerdict | None = None,
    now: datetime | None = None,
) -> tuple[RiskFlag, ...]:
    """Collect every applicable risk flag for a candidate opportunity."""
    reference = now or datetime.now(UTC)
    flags: set[RiskFlag] = {RiskFlag.NON_ATOMIC_EXECUTION}

    if not pair.is_tradable_pair:
        flags.add(RiskFlag.UNAPPROVED_PAIR)
    if pair.has_material_differences:
        flags.add(RiskFlag.RULES_DIFFERENCES_PRESENT)

    if kalshi_book.is_stale or (kalshi_freshness is not None and not kalshi_freshness.is_fresh):
        flags.add(RiskFlag.STALE_BOOK_KALSHI)
    if polymarket_book.is_stale or (
        polymarket_freshness is not None and not polymarket_freshness.is_fresh
    ):
        flags.add(RiskFlag.STALE_BOOK_POLYMARKET)

    # Polymarket's market channel carries no sequence numbers, so continuity of the
    # stream cannot be proven. Surface that on every opportunity touching it.
    if polymarket_book.sequence is None:
        flags.add(RiskFlag.NO_SEQUENCE_NUMBERS)

    for book in (kalshi_book, polymarket_book):
        levels = len(book.yes.asks) + len(book.no.asks)
        if levels <= THIN_BOOK_LEVELS:
            flags.add(RiskFlag.THIN_BOOK)
        spread = _spread(book)
        if spread is not None and spread > WIDE_SPREAD:
            flags.add(RiskFlag.WIDE_SPREAD)

    if kalshi_market.settlement_asset is not polymarket_market.settlement_asset:
        flags.add(RiskFlag.USD_USDC_BASIS)
    if SettlementAsset.USDC in {kalshi_market.settlement_asset, polymarket_market.settlement_asset}:
        flags.add(RiskFlag.USD_USDC_BASIS)

    closes = [m.close_time for m in (kalshi_market, polymarket_market) if m.close_time]
    if len(closes) == 2 and abs((closes[0] - closes[1]).total_seconds()) > 300:
        flags.add(RiskFlag.CLOSE_TIME_MISMATCH)
    for close_time in closes:
        if 0 <= (close_time - reference).total_seconds() <= NEAR_CLOSE_MINUTES * 60:
            flags.add(RiskFlag.NEAR_CLOSE)

    return tuple(sorted(flags, key=lambda flag: flag.value))


def single_level_flag(kalshi_levels: int, polymarket_levels: int) -> tuple[RiskFlag, ...]:
    """Flag an opportunity whose entire size rests on one price level per leg."""
    if kalshi_levels <= 1 and polymarket_levels <= 1:
        return (RiskFlag.SINGLE_LEVEL_FILL,)
    return ()
