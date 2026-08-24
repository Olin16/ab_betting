"""Simulated two-leg execution.

The execution is deliberately **sequential**, because that is what really happens: there
is no atomic cross-venue transaction between Kalshi and Polymarket. Leg 1 is sent, it
fills (or partially fills), and only then does leg 2 go out — into a book that has had
time to move.

This ordering is the whole point of paper trading here. The interesting outcomes are not
the clean double fills; they are:

- leg 1 fills, leg 2 fails entirely  -> fully unhedged directional exposure;
- leg 1 fills 100, leg 2 fills 60    -> 40 contracts unhedged;
- leg 2's price moved                -> the realized spread is worse than the detected one.

Every one of those is recorded on the resulting :class:`PaperTrade` rather than being
retried away, so the daily report reflects what the strategy would actually have suffered.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.config import PaperSettings
from event_arb.domain.enums import (
    ArbDirection,
    Outcome,
    PaperTradeStatus,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import Opportunity, OrderBook, VenueMarket
from event_arb.domain.money import CENT_EXP, ONE_DOLLAR, ZERO, ceil_to, quantize_price, quantize_qty
from event_arb.fees import taker_fee_for
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.paper.fills import SimulatedOrder, simulate_buy
from event_arb.venues.kalshi.fees import KalshiOrderFeeAccumulator

__all__ = ["PaperExecutor", "PaperTrade"]

log = get_logger(__name__)

#: Injected so tests can advance simulated time without sleeping.
BookProvider = Callable[[Venue, str, int], Awaitable[OrderBook | None]]


@dataclass
class PaperTrade:
    """The full record of one simulated arbitrage attempt."""

    id: str
    pair_id: str
    direction: ArbDirection
    opportunity: Opportunity
    leg1: SimulatedOrder | None = None
    leg2: SimulatedOrder | None = None
    status: PaperTradeStatus = PaperTradeStatus.PENDING
    leg1_latency_ms: int = 0
    leg2_latency_ms: int = 0
    kalshi_fees: Decimal = ZERO
    polymarket_fees: Decimal = ZERO
    settlement_result: dict[str, object] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    settled_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ exposure

    @property
    def hedged_quantity(self) -> Decimal:
        """Quantity covered on *both* legs — the only genuinely arbitraged size."""
        if self.leg1 is None or self.leg2 is None:
            return ZERO
        return quantize_qty(min(self.leg1.filled_quantity, self.leg2.filled_quantity))

    @property
    def unhedged_quantity(self) -> Decimal:
        """Quantity held on one venue with no offsetting position on the other.

        This is a naked directional position. It is the number that matters most in a
        paper-trading report, because it is the loss the strategy was not designed to take.
        """
        leg1_filled = self.leg1.filled_quantity if self.leg1 else ZERO
        leg2_filled = self.leg2.filled_quantity if self.leg2 else ZERO
        return quantize_qty(abs(leg1_filled - leg2_filled))

    @property
    def unhedged_venue(self) -> Venue | None:
        leg1_filled = self.leg1.filled_quantity if self.leg1 else ZERO
        leg2_filled = self.leg2.filled_quantity if self.leg2 else ZERO
        if leg1_filled == leg2_filled:
            return None
        return Venue.KALSHI if leg1_filled > leg2_filled else Venue.POLYMARKET

    @property
    def total_cost(self) -> Decimal:
        cost = ZERO
        for leg in (self.leg1, self.leg2):
            if leg is not None:
                cost += leg.gross_cost
        return quantize_price(cost)

    @property
    def total_fees(self) -> Decimal:
        return quantize_price(self.kalshi_fees + self.polymarket_fees)

    # ------------------------------------------------------------------- results

    @property
    def guaranteed_payout(self) -> Decimal:
        """Only the hedged portion pays $1.00 regardless of outcome."""
        return quantize_price(self.hedged_quantity * ONE_DOLLAR)

    @property
    def realized_pnl_if_hedged(self) -> Decimal:
        """P&L assuming the unhedged remainder settles worthless.

        This is the pessimistic view and the one reported by default: an unhedged leg is
        a coin flip, and a paper-trading report that assumes coin flips land favourably
        is worse than no report.
        """
        return quantize_price(self.guaranteed_payout - self.total_cost - self.total_fees)

    @property
    def realized_spread(self) -> Decimal:
        """Actual spread achieved, for comparison with the detected spread."""
        if self.hedged_quantity <= ZERO:
            return ZERO
        return quantize_price((self.guaranteed_payout - self.total_cost) / self.hedged_quantity)

    @property
    def slippage_vs_detected(self) -> Decimal:
        """How much worse execution was than detection predicted, per contract."""
        detected = self.opportunity
        if detected.executable_quantity <= ZERO or self.hedged_quantity <= ZERO:
            return ZERO
        expected_per_unit = (
            detected.expected_payout - detected.gross_acquisition_cost
        ) / detected.executable_quantity
        return quantize_price(expected_per_unit - self.realized_spread)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pair_id": self.pair_id,
            "direction": self.direction.value,
            "status": self.status.value,
            "hedged_quantity": str(self.hedged_quantity),
            "unhedged_quantity": str(self.unhedged_quantity),
            "unhedged_venue": self.unhedged_venue.value if self.unhedged_venue else None,
            "total_cost": str(self.total_cost),
            "total_fees": str(self.total_fees),
            "realized_pnl_if_hedged": str(self.realized_pnl_if_hedged),
            "detected_net_profit": str(self.opportunity.net_profit),
            "realized_spread": str(self.realized_spread),
            "slippage_vs_detected": str(self.slippage_vs_detected),
            "leg1_latency_ms": self.leg1_latency_ms,
            "leg2_latency_ms": self.leg2_latency_ms,
            "created_at": self.created_at.isoformat(),
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "settlement_result": self.settlement_result,
            "notes": list(self.notes),
        }


class PaperExecutor:
    """Simulates two-leg execution against evolving order books."""

    def __init__(
        self,
        settings: PaperSettings,
        book_provider: BookProvider,
        kalshi_direct_member: bool = False,
        freshness: FreshnessPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._book_provider = book_provider
        self._kalshi_direct_member = kalshi_direct_member
        self._freshness = freshness or FreshnessPolicy()

    def _usable(
        self, book: OrderBook | None, venue: Venue, market_id: str
    ) -> tuple[OrderBook | None, str]:
        """Validate a book at simulated arrival time.

        A book that the opportunity engine would have refused must not be filled against
        here either. Paper results feed the live-readiness gate in docs/operations.md, so
        a simulator that trades on stale or mismatched books lets that gate certify
        itself on evidence it should have thrown away.
        """
        if book is None:
            return None, f"no {venue.value} book at arrival time"
        if book.venue is not venue or book.market_id != market_id:
            return None, (
                f"book mismatch at arrival: expected {venue.value}/{market_id}, "
                f"got {book.venue.value}/{book.market_id}"
            )
        verdict = self._freshness.evaluate(book)
        if not verdict.is_fresh:
            return None, f"{venue.value} book unusable at arrival: {verdict.reason}"
        return book, ""

    async def execute(
        self,
        opportunity: Opportunity,
        kalshi_market: VenueMarket,
        polymarket_market: VenueMarket,
    ) -> PaperTrade:
        """Run one simulated arbitrage attempt, leg 1 then leg 2."""
        trade = PaperTrade(
            id=str(uuid.uuid4()),
            pair_id=opportunity.pair_id,
            direction=opportunity.direction,
            opportunity=opportunity,
            leg1_latency_ms=self._settings.leg1_latency_ms,
            leg2_latency_ms=self._settings.leg2_latency_ms,
        )

        # --- Leg 1: Kalshi ------------------------------------------------------
        kalshi_book, problem = self._usable(
            await self._book_provider(
                Venue.KALSHI, opportunity.kalshi_leg.market_id, self._settings.leg1_latency_ms
            ),
            Venue.KALSHI,
            opportunity.kalshi_leg.market_id,
        )
        if kalshi_book is None:
            # Nothing was sent, so there is no exposure — not a failed hedge.
            trade.status = PaperTradeStatus.PENDING
            trade.notes.append(f"leg 1 not attempted: {problem}")
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.leg1 = simulate_buy(
            book=kalshi_book,
            outcome=opportunity.kalshi_leg.outcome,
            quantity=opportunity.executable_quantity,
            client_order_id=f"{trade.id}-leg1",
            # Never pay more than the detected worst price: a real order would be a limit.
            limit_price=opportunity.kalshi_leg.worst_price,
            adverse_pct=self._settings.adverse_fill_pct,
        )
        trade.kalshi_fees = self._kalshi_fee_for(trade.leg1, kalshi_market)

        if trade.leg1.is_empty:
            trade.status = PaperTradeStatus.PENDING
            trade.notes.append("leg 1 did not fill; no exposure taken")
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.status = PaperTradeStatus.LEG1_FILLED
        if not trade.leg1.is_complete:
            trade.notes.append(
                f"leg 1 partially filled: {trade.leg1.filled_quantity} of "
                f"{trade.leg1.requested_quantity}"
            )

        # --- Leg 2: Polymarket, into a book that has moved -----------------------
        # Only hedge what leg 1 actually got. Sizing leg 2 to the original quantity
        # would create exposure in the opposite direction.
        hedge_quantity = trade.leg1.filled_quantity

        polymarket_book, problem = self._usable(
            await self._book_provider(
                Venue.POLYMARKET,
                opportunity.polymarket_leg.market_id,
                self._settings.leg2_latency_ms,
            ),
            Venue.POLYMARKET,
            opportunity.polymarket_leg.market_id,
        )
        if polymarket_book is None:
            # Leg 1 is already on the book, so this is genuine unhedged exposure.
            trade.status = PaperTradeStatus.LEG2_FAILED
            trade.notes.append(
                f"leg 2 failed: {problem}; {trade.unhedged_quantity} contracts unhedged"
            )
            metrics.paper_trades.labels(status=trade.status.value).inc()
            return trade

        trade.leg2 = simulate_buy(
            book=polymarket_book,
            outcome=opportunity.polymarket_leg.outcome,
            quantity=hedge_quantity,
            client_order_id=f"{trade.id}-leg2",
            limit_price=opportunity.polymarket_leg.worst_price,
            adverse_pct=self._settings.adverse_fill_pct,
        )
        trade.polymarket_fees = self._polymarket_fee_for(trade.leg2, polymarket_market)

        if trade.leg2.is_empty:
            trade.status = PaperTradeStatus.LEG2_FAILED
            trade.notes.append(
                f"leg 2 did not fill; {trade.unhedged_quantity} contracts unhedged on Kalshi"
            )
        elif trade.leg2.filled_quantity < hedge_quantity:
            trade.status = PaperTradeStatus.PARTIALLY_HEDGED
            trade.notes.append(
                f"leg 2 partially filled; {trade.unhedged_quantity} contracts unhedged"
            )
        else:
            trade.status = PaperTradeStatus.BOTH_LEGS_FILLED

        if trade.slippage_vs_detected > ZERO:
            trade.notes.append(
                f"realized spread {trade.slippage_vs_detected} worse per contract than detected"
            )

        metrics.paper_trades.labels(status=trade.status.value).inc()
        log.info(
            "paper.trade.executed",
            trade_id=trade.id,
            status=trade.status.value,
            hedged=str(trade.hedged_quantity),
            unhedged=str(trade.unhedged_quantity),
            pnl=str(trade.realized_pnl_if_hedged),
        )
        return trade

    def _kalshi_fee_for(self, order: SimulatedOrder, market: VenueMarket) -> Decimal:
        accumulator = KalshiOrderFeeAccumulator(
            config=market.fee_config, direct_member=self._kalshi_direct_member
        )
        total = sum(
            (accumulator.charge_fill(fill.quantity, fill.price).net for fill in order.fills), ZERO
        )
        return ceil_to(total, CENT_EXP)

    def _polymarket_fee_for(self, order: SimulatedOrder, market: VenueMarket) -> Decimal:
        return quantize_price(
            sum(
                (
                    taker_fee_for(fill.quantity, fill.price, market.fee_config)
                    for fill in order.fills
                ),
                ZERO,
            )
        )


def settle(
    trade: PaperTrade, winning_outcome: Outcome, settled_at: datetime | None = None
) -> PaperTrade:
    """Reconcile a trade against the real settlement outcome.

    The hedged portion pays $1.00 per contract by construction. The **unhedged** portion
    pays $1.00 only if the side we were left holding won, and $0.00 otherwise — this is
    where a partially-hedged trade discovers what its exposure really cost.
    """
    hedged_payout = trade.hedged_quantity * ONE_DOLLAR

    unhedged_payout = ZERO
    unhedged_venue = trade.unhedged_venue
    if trade.unhedged_quantity > ZERO and unhedged_venue is not None:
        leg = trade.leg1 if unhedged_venue is Venue.KALSHI else trade.leg2
        if leg is not None and leg.outcome is winning_outcome:
            unhedged_payout = trade.unhedged_quantity * ONE_DOLLAR

    total_payout = hedged_payout + unhedged_payout
    final_pnl = total_payout - trade.total_cost - trade.total_fees

    # Read the assets off the opportunity's legs rather than assuming a product.
    kalshi_asset = trade.opportunity.kalshi_leg.settlement_asset
    polymarket_asset = trade.opportunity.polymarket_leg.settlement_asset

    trade.settlement_result = {
        "winning_outcome": winning_outcome.value,
        "hedged_payout": str(quantize_price(hedged_payout)),
        "unhedged_payout": str(quantize_price(unhedged_payout)),
        "total_payout": str(quantize_price(total_payout)),
        "final_pnl": str(quantize_price(final_pnl)),
        "unhedged_venue": unhedged_venue.value if unhedged_venue else None,
        # Settlement assets are read from the legs, not assumed. Polymarket US settles in
        # USD and Polymarket Global in USDC, so hardcoding either misreports one of them.
        "kalshi_settlement_asset": kalshi_asset.value,
        "polymarket_settlement_asset": polymarket_asset.value,
        "settlement_assets_differ": kalshi_asset is not polymarket_asset,
    }

    if polymarket_asset is SettlementAsset.USDC:
        # Only the Global path carries conversion and depeg exposure.
        trade.settlement_result["usdc_denominated_payout"] = str(
            quantize_price(
                trade.hedged_quantity * ONE_DOLLAR
                if trade.leg2 and trade.leg2.outcome is winning_outcome
                else ZERO
            )
        )
        trade.settlement_result["settlement_asset_note"] = (
            f"Polymarket proceeds settle in {SettlementAsset.USDC.value}, "
            f"Kalshi in {kalshi_asset.value}; conversion and depeg risk apply"
        )
    else:
        trade.settlement_result["settlement_asset_note"] = (
            f"Both legs settle in {polymarket_asset.value}; "
            "no cross-asset conversion or depeg exposure"
        )

    trade.status = PaperTradeStatus.SETTLED
    trade.settled_at = settled_at or datetime.now(UTC)
    return trade
