"""Settlement reconciliation for paper trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from event_arb.domain.enums import Outcome
from event_arb.domain.money import ZERO, quantize_price
from event_arb.paper.executor import PaperTrade, settle

__all__ = ["SettlementReconciliation", "reconcile"]


@dataclass(frozen=True)
class SettlementReconciliation:
    """Comparison of predicted vs actual outcome for a batch of settled trades."""

    settled: int
    predicted_pnl: Decimal
    actual_pnl: Decimal
    unhedged_wins: int
    unhedged_losses: int

    @property
    def variance(self) -> Decimal:
        """Actual minus predicted. Non-zero variance comes from unhedged exposure."""
        return quantize_price(self.actual_pnl - self.predicted_pnl)


def reconcile(
    trades: list[PaperTrade],
    outcomes: dict[str, Outcome],
    settled_at: datetime | None = None,
) -> SettlementReconciliation:
    """Settle each trade against its pair's real outcome and summarize the result.

    ``outcomes`` maps ``pair_id`` to the outcome that actually won. Trades whose pair has
    no recorded outcome are skipped rather than assumed.
    """
    settled_count = 0
    predicted = ZERO
    actual = ZERO
    unhedged_wins = 0
    unhedged_losses = 0

    for trade in trades:
        winning = outcomes.get(trade.pair_id)
        if winning is None:
            continue
        predicted += trade.realized_pnl_if_hedged
        settle(trade, winning, settled_at)
        result = trade.settlement_result or {}
        actual += Decimal(str(result.get("final_pnl", "0")))
        settled_count += 1

        if trade.unhedged_quantity > ZERO:
            if Decimal(str(result.get("unhedged_payout", "0"))) > ZERO:
                unhedged_wins += 1
            else:
                unhedged_losses += 1

    return SettlementReconciliation(
        settled=settled_count,
        predicted_pnl=quantize_price(predicted),
        actual_pnl=quantize_price(actual),
        unhedged_wins=unhedged_wins,
        unhedged_losses=unhedged_losses,
    )
