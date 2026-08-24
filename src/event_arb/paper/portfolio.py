"""Paper portfolio: balances, exposure, and daily reporting.

USD and USDC are tracked as **separate balances**. Netting them into a single "cash"
number would hide exactly the risk this project is supposed to surface: a Kalshi profit
and a Polymarket loss do not offset until someone converts, at a rate that is not
guaranteed to be 1.0000.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from event_arb.domain.enums import PaperTradeStatus, SettlementAsset, Venue
from event_arb.domain.models import RejectedOpportunity
from event_arb.domain.money import ZERO, quantize_price
from event_arb.paper.evidence import EvidenceGrade
from event_arb.paper.executor import PaperTrade

__all__ = ["DailyReport", "PaperPortfolio"]


@dataclass
class DailyReport:
    """One day's paper-trading summary."""

    day: date
    trades: int = 0
    fully_hedged: int = 0
    partially_hedged: int = 0
    leg2_failures: int = 0
    total_pnl: Decimal = ZERO
    total_fees: Decimal = ZERO
    total_unhedged_quantity: Decimal = ZERO
    detected_profit: Decimal = ZERO
    missed_opportunities: dict[str, int] = field(default_factory=dict)
    #: Trades by leg-2 evidence grade. Only ``measured`` trades observed a book that
    #: actually moved between legs; the rest cannot support a hedge-rate claim.
    evidence: dict[str, int] = field(default_factory=dict)

    @property
    def measured_trades(self) -> int:
        return self.evidence.get(EvidenceGrade.MEASURED.value, 0)

    @property
    def hedge_rate_is_measured(self) -> bool:
        """True only when every counted trade re-observed a book that had moved.

        Read :attr:`hedge_success_rate` alongside this. A 100% rate over trades that all
        re-read an unchanged book means the book never moved in the sample — it is not
        evidence that hedging survives adverse movement, and quoting it as such would
        overstate readiness in exactly the direction that costs money.
        """
        return self.trades > 0 and self.measured_trades == self.trades

    @property
    def evidence_caveat(self) -> str:
        """One line stating what the hedge rate does and does not show."""
        if self.trades == 0:
            return "no trades"
        if self.hedge_rate_is_measured:
            return "measured: every leg 2 priced against a book observed to have moved"
        unmeasured = self.trades - self.measured_trades
        return (
            f"NOT measured: {unmeasured} of {self.trades} trades priced leg 2 against a book "
            "that did not move (or was modelled); this rate does not show hedging surviving "
            "adverse movement"
        )

    @property
    def hedge_success_rate(self) -> Decimal:
        """Fraction of attempts that ended fully hedged.

        This, not P&L, is the headline number for a cross-venue strategy: an unhedged
        leg is the failure mode that turns arbitrage into gambling.

        It is only meaningful when :attr:`hedge_rate_is_measured` is true. See
        :attr:`evidence_caveat`.
        """
        if self.trades == 0:
            return ZERO
        return quantize_price(Decimal(self.fully_hedged) / Decimal(self.trades) * Decimal("100"))

    @property
    def realization_rate(self) -> Decimal:
        """Realized P&L as a percentage of the profit detection predicted.

        This can exceed 100%, and that is not a bug: detection subtracts the slippage,
        operational, and USDC-conversion **buffers**, which are safety margins rather
        than costs a clean fill actually incurs. A rate below 100% is the meaningful
        signal — it means execution gave back edge that detection had counted on.
        """
        if self.detected_profit <= ZERO:
            return ZERO
        return quantize_price(self.total_pnl / self.detected_profit * Decimal("100"))

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "trades": self.trades,
            "fully_hedged": self.fully_hedged,
            "partially_hedged": self.partially_hedged,
            "leg2_failures": self.leg2_failures,
            "hedge_success_rate_pct": str(self.hedge_success_rate),
            "hedge_rate_is_measured": self.hedge_rate_is_measured,
            "evidence_caveat": self.evidence_caveat,
            "evidence": dict(self.evidence),
            "total_pnl": str(quantize_price(self.total_pnl)),
            "total_fees": str(quantize_price(self.total_fees)),
            "detected_profit": str(quantize_price(self.detected_profit)),
            "realization_rate_pct": str(self.realization_rate),
            "total_unhedged_quantity": str(self.total_unhedged_quantity),
            "missed_opportunities": dict(self.missed_opportunities),
        }


class PaperPortfolio:
    """Tracks simulated balances, positions, and trade history."""

    def __init__(
        self, starting_usd: Decimal = Decimal("10000"), starting_usdc: Decimal = Decimal("10000")
    ) -> None:
        self._balances: dict[SettlementAsset, Decimal] = {
            SettlementAsset.USD: starting_usd,
            SettlementAsset.USDC: starting_usdc,
        }
        self._starting = dict(self._balances)
        self.trades: list[PaperTrade] = []
        self.rejections: list[RejectedOpportunity] = []

    # ------------------------------------------------------------------ balances

    def balance(self, asset: SettlementAsset) -> Decimal:
        return quantize_price(self._balances[asset])

    @property
    def balances(self) -> dict[str, str]:
        return {asset.value: str(self.balance(asset)) for asset in self._balances}

    def has_capacity(self, asset: SettlementAsset, amount: Decimal) -> bool:
        return self._balances[asset] >= amount

    def record(self, trade: PaperTrade) -> None:
        """Apply a completed trade's cash effects to the simulated balances."""
        self.trades.append(trade)
        if trade.leg1 is not None:
            self._balances[SettlementAsset.USD] -= trade.leg1.gross_cost + trade.kalshi_fees
        if trade.leg2 is not None:
            self._balances[SettlementAsset.USDC] -= trade.leg2.gross_cost + trade.polymarket_fees

    def record_rejections(self, rejections: list[RejectedOpportunity]) -> None:
        self.rejections.extend(rejections)

    # ------------------------------------------------------------------ exposure

    def open_exposure(self) -> list[dict[str, object]]:
        """Unhedged positions still outstanding, by trade."""
        exposure: list[dict[str, object]] = []
        for trade in self.trades:
            if trade.status is PaperTradeStatus.SETTLED or trade.unhedged_quantity <= ZERO:
                continue
            venue = trade.unhedged_venue
            leg = trade.leg1 if venue is Venue.KALSHI else trade.leg2
            exposure.append(
                {
                    "trade_id": trade.id,
                    "pair_id": trade.pair_id,
                    "venue": venue.value if venue else None,
                    "outcome": leg.outcome.value if leg else None,
                    "quantity": str(trade.unhedged_quantity),
                    "cost_basis": str(leg.vwap) if leg else "0",
                    "status": trade.status.value,
                }
            )
        return exposure

    @property
    def total_unhedged_quantity(self) -> Decimal:
        return sum(
            (t.unhedged_quantity for t in self.trades if t.status is not PaperTradeStatus.SETTLED),
            ZERO,
        )

    # ------------------------------------------------------------------ reporting

    def daily_report(self, day: date | None = None) -> DailyReport:
        target = day or datetime.now(UTC).date()
        report = DailyReport(day=target)

        for trade in self.trades:
            if trade.created_at.date() != target:
                continue
            report.trades += 1
            report.total_pnl += trade.realized_pnl_if_hedged
            report.total_fees += trade.total_fees
            report.total_unhedged_quantity += trade.unhedged_quantity
            report.detected_profit += trade.opportunity.net_profit
            grade = trade.leg2_evidence.value
            report.evidence[grade] = report.evidence.get(grade, 0) + 1
            if trade.status is PaperTradeStatus.BOTH_LEGS_FILLED:
                report.fully_hedged += 1
            elif trade.status is PaperTradeStatus.PARTIALLY_HEDGED:
                report.partially_hedged += 1
            elif trade.status is PaperTradeStatus.LEG2_FAILED:
                report.leg2_failures += 1

        missed: dict[str, int] = defaultdict(int)
        for rejection in self.rejections:
            if rejection.detected_at.date() == target:
                missed[rejection.reason.value] += 1
        report.missed_opportunities = dict(missed)
        return report

    # -------------------------------------------------------------------- export

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "balances": self.balances,
                "starting_balances": {a.value: str(v) for a, v in self._starting.items()},
                "open_exposure": self.open_exposure(),
                "trades": [t.as_dict() for t in self.trades],
                "daily_report": self.daily_report().as_dict(),
            },
            indent=indent,
        )

    def to_csv(self) -> str:
        """Flat per-trade CSV for spreadsheet review."""
        buffer = io.StringIO()
        columns = [
            "id",
            "pair_id",
            "direction",
            "status",
            "hedged_quantity",
            "unhedged_quantity",
            "unhedged_venue",
            "total_cost",
            "total_fees",
            "realized_pnl_if_hedged",
            "detected_net_profit",
            "realized_spread",
            "slippage_vs_detected",
            "created_at",
            "settled_at",
        ]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for trade in self.trades:
            writer.writerow({k: v for k, v in trade.as_dict().items() if k in columns})
        return buffer.getvalue()
