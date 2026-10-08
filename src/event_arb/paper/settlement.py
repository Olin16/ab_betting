"""Settlement reconciliation for paper trades, and the workflow that records one.

Settlement arithmetic already existed here and in :func:`event_arb.paper.executor.settle`,
but nothing called it: no runtime, CLI or API path recorded an outcome, so a computed
payout of $100 changed no balance and the portfolio only ever went down. A paper trial
that never credits a settlement measures half a strategy.

:func:`record_settlement` is the missing half. It is deliberately **operator-driven**: an
outcome is entered by a named person with the settlement authority they read, rather than
ingested automatically from a venue. Automatic ingestion is a larger surface — an outcome
feed that is wrong, late, or disagreeing between venues would silently rewrite the trial's
own P&L — and a first one-pair trial does not need it. What it does need is that the
outcome is attributable, credited exactly once, and durable, which is what this provides.

Crediting exactly once is structural rather than procedural: every ledger entry the credit
produces has a deterministic id derived from the trade and account, so a repeated command,
a retried transaction or a replayed startup all converge on the same single credit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from event_arb.domain.enums import Outcome
from event_arb.domain.money import ZERO, quantize_price
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.paper.executor import PaperTrade, SettlementOutcomes, settle
from event_arb.paper.ledger import LedgerEntry
from event_arb.paper.portfolio import PaperPortfolio

__all__ = [
    "SettlementError",
    "SettlementReconciliation",
    "SettlementRecord",
    "apply_settlement",
    "reconcile",
]

log = get_logger(__name__)


class SettlementError(Exception):
    """A settlement could not be recorded as asked."""


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


@dataclass(frozen=True)
class SettlementRecord:
    """One trade's settlement, and the cash it moved."""

    trade: PaperTrade
    entries: tuple[LedgerEntry, ...]
    already_settled: bool

    @property
    def credited(self) -> Decimal:
        return quantize_price(sum((e.amount for e in self.entries), ZERO))

    @property
    def final_pnl(self) -> Decimal:
        result = self.trade.settlement_result or {}
        return Decimal(str(result.get("final_pnl", "0")))

    @property
    def hedge_held(self) -> bool:
        result = self.trade.settlement_result or {}
        return bool(result.get("hedge_held", True))

    def as_dict(self) -> dict[str, object]:
        return {
            "trade_id": self.trade.id,
            "pair_id": self.trade.pair_id,
            "already_settled": self.already_settled,
            "execution_status": self.trade.execution_status.value,
            "credited": str(self.credited),
            "final_pnl": str(self.final_pnl),
            "hedge_held": self.hedge_held,
            "entries": [e.as_dict() for e in self.entries],
            "settlement_result": self.trade.settlement_result,
        }


def apply_settlement(
    portfolio: PaperPortfolio,
    trade: PaperTrade,
    outcomes: SettlementOutcomes,
    settled_at: datetime | None = None,
) -> SettlementRecord:
    """Settle one trade and credit its payouts to the accounts that received them.

    Refuses to re-settle a trade that already carries a settlement. Re-running the command
    is not an error — an operator who is unsure whether the first attempt landed should be
    able to repeat it — so this returns the existing record rather than raising, and
    credits nothing further.
    """
    if trade.is_settled:
        return SettlementRecord(
            trade=trade, entries=tuple(portfolio.credit_settlement(trade)), already_settled=True
        )

    settle(trade, outcomes, settled_at)
    entries = tuple(portfolio.credit_settlement(trade))
    result = trade.settlement_result or {}
    hedge_held = bool(result.get("hedge_held", True))
    metrics.paper_settlements.labels(hedge_held=str(hedge_held).lower()).inc()
    log.info(
        "paper.settlement.recorded",
        trade_id=trade.id,
        pair_id=trade.pair_id,
        kalshi_outcome=outcomes.kalshi.value,
        polymarket_outcome=outcomes.polymarket.value,
        payouts=result.get("payouts_by_account"),
        final_pnl=result.get("final_pnl"),
        hedge_held=hedge_held,
        recorded_by=outcomes.recorded_by,
    )
    return SettlementRecord(trade=trade, entries=entries, already_settled=False)


def reconcile(
    trades: list[PaperTrade],
    outcomes: dict[str, Outcome] | dict[str, SettlementOutcomes],
    settled_at: datetime | None = None,
    portfolio: PaperPortfolio | None = None,
) -> SettlementReconciliation:
    """Settle each trade against its pair's real outcome and summarize the result.

    ``outcomes`` maps ``pair_id`` to the outcome that actually won, either as a single
    :class:`Outcome` or as a :class:`SettlementOutcomes` carrying one per venue. Trades
    whose pair has no recorded outcome are skipped rather than assumed.

    Passing ``portfolio`` credits the payouts as well as computing them. Omitting it
    computes only, which is what a what-if reconciliation wants — the distinction used to
    not exist, and computing was all this ever did.
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
        resolved = (
            winning
            if isinstance(winning, SettlementOutcomes)
            else SettlementOutcomes(kalshi=winning, polymarket=winning)
        )
        if portfolio is not None:
            record = apply_settlement(portfolio, trade, resolved, settled_at)
            result = record.trade.settlement_result or {}
        else:
            settle(trade, resolved, settled_at)
            result = trade.settlement_result or {}
        actual += Decimal(str(result.get("final_pnl", "0")))
        settled_count += 1

        if trade.unhedged_quantity > ZERO:
            unhedged_leg = "kalshi" if trade.unhedged_venue is not None else ""
            legs = result.get("legs")
            won_unhedged = False
            if isinstance(legs, list):
                # The unhedged remainder sits on whichever venue filled more, so its
                # payout is that leg's. Reading it from the per-leg breakdown keeps this
                # correct for an inverted pair, which a single shared winner did not.
                for leg in legs:
                    if not isinstance(leg, dict):
                        continue
                    if trade.unhedged_venue is not None and leg.get("leg") == (
                        "kalshi" if trade.unhedged_venue.value == "KALSHI" else "polymarket"
                    ):
                        won_unhedged = bool(leg.get("won"))
                        unhedged_leg = str(leg.get("leg", ""))
            if won_unhedged:
                unhedged_wins += 1
            else:
                unhedged_losses += 1
            log.debug("paper.settlement.unhedged", leg=unhedged_leg, won=won_unhedged)

    return SettlementReconciliation(
        settled=settled_count,
        predicted_pnl=quantize_price(predicted),
        actual_pnl=quantize_price(actual),
        unhedged_wins=unhedged_wins,
        unhedged_losses=unhedged_losses,
    )
