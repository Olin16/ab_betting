"""Per-account reconciliation of a paper run: does the cash still add up?

A paper trial's headline numbers — hedge rate, P&L, exposure — are only worth reading if
the accounts they came from balance. This module answers the one arithmetic question that
makes the rest legible:

    opening balance + every durable cash entry == ending balance, per account

It has to be per account and not in aggregate. A USD gain and a USDC loss net to something
meaningless, and a debit landing in the wrong account is exactly the class of defect that
totals hide: leg 2 used to be charged to USDC unconditionally, so every Polymarket US
trade drew down a balance no US account holds while the aggregate stayed plausible.

Everything here is derived from the ledger the run actually wrote, never from a running
total kept alongside it. That is the point: a total cannot be checked, and reconciling a
run against a number the run itself maintained proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.domain.money import ZERO, quantize_price
from event_arb.paper.accounts import AccountId
from event_arb.paper.durability import InvalidatedAttempt
from event_arb.paper.integrity import IntegrityReport, verify_trades
from event_arb.paper.ledger import EntryKind
from event_arb.paper.portfolio import PaperPortfolio

__all__ = ["AccountReconciliation", "ReconciliationReport", "reconcile_accounts"]


@dataclass(frozen=True)
class AccountReconciliation:
    """One account's arithmetic, stated so a reader can check it by hand."""

    account: str
    opening: Decimal
    trade_debits: Decimal
    fee_debits: Decimal
    settlement_credits: Decimal
    carried_forward: Decimal
    ending: Decimal
    reserved: Decimal
    available: Decimal
    entry_count: int

    @property
    def movements(self) -> Decimal:
        """Every cash entry after the opening balance."""
        return quantize_price(
            self.trade_debits + self.fee_debits + self.settlement_credits + self.carried_forward
        )

    @property
    def balances(self) -> bool:
        """True when opening plus movements equals the ending balance, to the cent."""
        return quantize_price(self.opening + self.movements) == quantize_price(self.ending)

    @property
    def holds_are_sane(self) -> bool:
        """Reserved cash is never negative — a release without a hold used to make it so.

        That was not cosmetic: negative ``reserved`` inflated ``available`` by the whole
        cost of a trade, and the capital gate went on reading the opening balance no
        matter how much had been spent.
        """
        return self.reserved >= ZERO

    def as_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "opening": str(self.opening),
            "trade_debits": str(self.trade_debits),
            "fee_debits": str(self.fee_debits),
            "settlement_credits": str(self.settlement_credits),
            "carried_forward": str(self.carried_forward),
            "movements": str(self.movements),
            "ending": str(self.ending),
            "reserved": str(self.reserved),
            "available": str(self.available),
            "ledger_entries": self.entry_count,
            "balances": self.balances,
            "check": f"{self.opening} + {self.movements} = {self.ending}",
        }


@dataclass
class ReconciliationReport:
    """What a run did to its accounts, and whether the books still close.

    ``reconciles`` is the gate an acceptance exercise reads. It is deliberately *not* the
    same question as "did the run go well": a run that stopped on its first cycle can
    reconcile perfectly, and a run that traded profitably all day cannot be trusted if it
    does not.
    """

    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    accounts: list[AccountReconciliation] = field(default_factory=list)
    open_positions: list[dict[str, object]] = field(default_factory=list)
    outstanding_holds: list[dict[str, object]] = field(default_factory=list)
    rejected_attempts: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, int] = field(default_factory=dict)
    invalidated: list[InvalidatedAttempt] = field(default_factory=list)
    #: Each retained trade's cash checked against the ledger that should hold it. The
    #: per-account arithmetic above cannot do this: both its sides are sums of the same
    #: ledger, so an entry deleted from storage disappears from both and the identity
    #: still closes. See :mod:`event_arb.paper.integrity`.
    integrity: IntegrityReport = field(default_factory=IntegrityReport)
    #: Earlier runs that never recorded a clean shutdown, from the journal. What they did
    #: is unknown by construction, so they are a gap in this evidence like any other.
    unfinished_runs: list[str] = field(default_factory=list)
    trades: int = 0
    settled_trades: int = 0
    total_unhedged_quantity: Decimal = ZERO
    #: Filled in by the caller: why the run ended, in its own words.
    stop_causes: list[str] = field(default_factory=list)

    @property
    def reconciles(self) -> bool:
        """The accounts close, no hold went negative, and every trade's cash is there.

        The third clause is not redundant with the first. Per-account arithmetic compares
        a sum of the ledger against a sum of the same ledger, so removing a trade's debits
        removes them from both sides at once and the identity survives intact — which is
        how a database missing a trade's entire cost reported itself balanced.
        """
        return (
            all(a.balances and a.holds_are_sane for a in self.accounts) and self.integrity.is_sound
        )

    @property
    def evidence_is_complete(self) -> bool:
        """False when anything happened that these figures cannot account for.

        Three separate ways that can be true, kept separate because they need different
        responses: an attempt storage refused, a run that died without shutting down, and
        a stored trade whose cash is not in the ledger.
        """
        return not self.invalidated and not self.unfinished_runs and self.integrity.is_sound

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "reconciles": self.reconciles,
            "evidence_is_complete": self.evidence_is_complete,
            "accounts": [a.as_dict() for a in self.accounts],
            "trades": self.trades,
            "settled_trades": self.settled_trades,
            "total_unhedged_quantity": str(self.total_unhedged_quantity),
            "open_positions": list(self.open_positions),
            "outstanding_holds": list(self.outstanding_holds),
            "rejected_attempts": dict(self.rejected_attempts),
            "evidence": dict(self.evidence),
            "invalidated_attempts": [a.as_dict() for a in self.invalidated],
            "unfinished_runs": list(self.unfinished_runs),
            "integrity": self.integrity.as_dict(),
            "stop_causes": list(self.stop_causes),
        }

    def to_text(self) -> str:
        """The same figures as something an operator can read in a terminal."""
        lines = [
            f"Paper reconciliation — {self.generated_at.isoformat()}",
            f"  reconciles          : {self.reconciles}",
            f"  evidence complete   : {self.evidence_is_complete}",
            f"  trades              : {self.trades} ({self.settled_trades} settled)",
            f"  open unhedged qty   : {self.total_unhedged_quantity}",
            "",
            "  per account (opening + movements = ending):",
        ]
        for account in self.accounts:
            mark = "ok " if account.balances else "OFF"
            lines.append(
                f"    [{mark}] {account.account:<22} {account.opening} "
                f"{account.movements:+} = {account.ending}   "
                f"reserved={account.reserved} available={account.available}"
            )
        lines.append("")
        lines.append(self.integrity.render())
        if self.unfinished_runs:
            lines.append("")
            lines.append(
                f"  UNFINISHED RUNS     : {len(self.unfinished_runs)} earlier run(s) ended "
                "without a clean shutdown"
            )
            lines.extend(f"    {run_id}" for run_id in self.unfinished_runs)
            lines.append(
                "      -> what they attempted is unknown; review them, then "
                "`event-arb acknowledge-run <id>`"
            )
        if self.open_positions:
            lines.append("")
            lines.append(f"  open positions      : {len(self.open_positions)}")
            for position in self.open_positions:
                lines.append(
                    f"    {position['trade_id']}  {position['account']}  "
                    f"{position['quantity']} @ {position['cost_basis']}  "
                    f"({position['execution_status']})"
                )
        if self.outstanding_holds:
            lines.append("")
            lines.append(f"  outstanding holds   : {len(self.outstanding_holds)}")
        if self.rejected_attempts:
            lines.append("")
            lines.append(f"  refused attempts    : {self.rejected_attempts}")
        if self.evidence:
            lines.append(f"  leg-2 evidence      : {self.evidence}")
        if self.invalidated:
            lines.append("")
            lines.append(f"  INVALIDATED         : {len(self.invalidated)} attempt(s)")
            for attempt in self.invalidated:
                lines.append(f"    {attempt.trade_id} at {attempt.stage.value}: {attempt.error}")
                lines.append(f"      -> {attempt.consequence}")
        if self.stop_causes:
            lines.append("")
            lines.append("  stop causes:")
            lines.extend(f"    {cause}" for cause in self.stop_causes)
        return "\n".join(lines)


def reconcile_accounts(
    portfolio: PaperPortfolio, stop_causes: list[str] | None = None
) -> ReconciliationReport:
    """Recompute every account from its ledger entries and compare with its balance."""
    report = ReconciliationReport(stop_causes=list(stop_causes or []))

    by_kind: dict[tuple[AccountId, EntryKind], Decimal] = {}
    counted: dict[AccountId, int] = {}
    for entry in portfolio.ledger.entries:
        key = (entry.account, entry.kind)
        by_kind[key] = by_kind.get(key, ZERO) + entry.amount
        counted[entry.account] = counted.get(entry.account, 0) + 1

    def total(account: AccountId, kind: EntryKind) -> Decimal:
        return quantize_price(by_kind.get((account, kind), ZERO))

    for account in portfolio.ledger.accounts():
        report.accounts.append(
            AccountReconciliation(
                account=account.key,
                opening=total(account, EntryKind.OPENING_BALANCE),
                trade_debits=total(account, EntryKind.TRADE_DEBIT),
                fee_debits=total(account, EntryKind.FEE_DEBIT),
                settlement_credits=total(account, EntryKind.SETTLEMENT_CREDIT),
                carried_forward=total(account, EntryKind.CARRY_FORWARD),
                ending=portfolio.balance(account),
                reserved=portfolio.ledger.reserved(account),
                available=portfolio.available(account),
                entry_count=counted.get(account, 0),
            )
        )

    report.open_positions = portfolio.open_exposure()
    report.outstanding_holds = [
        {
            "trade_id": hold.trade_id,
            "account": hold.account.key,
            "amount": str(hold.amount),
            "taken_at": hold.created_at.isoformat(),
        }
        for hold in portfolio.ledger.outstanding_holds()
    ]
    rejected: dict[str, int] = {}
    for rejection in portfolio.rejections:
        rejected[rejection.reason.value] = rejected.get(rejection.reason.value, 0) + 1
    report.rejected_attempts = rejected

    evidence: dict[str, int] = {}
    for trade in portfolio.trades:
        grade = trade.leg2_evidence.value
        evidence[grade] = evidence.get(grade, 0) + 1
    report.evidence = evidence

    report.invalidated = list(portfolio.invalidated)
    report.unfinished_runs = list(portfolio.unfinished_runs)
    report.integrity = verify_trades(portfolio)
    report.trades = len(portfolio.trades)
    report.settled_trades = sum(1 for t in portfolio.trades if t.is_settled)
    report.total_unhedged_quantity = portfolio.total_unhedged_quantity
    return report
