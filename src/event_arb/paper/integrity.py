"""Does each stored trade's cash actually appear in the ledger?

:mod:`event_arb.paper.reconciliation` asks whether each account's arithmetic closes:
``opening + movements == ending``. That check has a blind spot big enough to drive a
corrupted database through, because both sides of it are computed from the same place.
``movements`` sums the ledger entries; ``ending`` is ``portfolio.balance``, which sums the
same ledger entries. Deleting a trade's two ``trade_debit`` rows removes them from *both*
sides at once, so the identity still holds — and the report says the books close while the
cash the trade spent has quietly reappeared. Reproduced on disposable storage: one trade
retained, its debits deleted, Kalshi cash back up from 9951.2500 to 9998.2500 and
Polymarket US from 9960.5900 to 9998.5900, ``reconciles=True``.

Closing that blind spot needs a second, *independent* source of truth about what should be
there. The trade rows are that source: they are stored separately from the ledger, and a
trade knows what it filled, at what price, on which account, and what it was paid at
settlement. So this module re-derives the entries each retained trade implies — using
:meth:`PaperPortfolio.plan_record`, the same deterministic rule the write path uses — and
checks the ledger actually contains them, with the right amount, on the right account.

What this does and does not prove. It compares two independently stored things and reports
where they disagree, which is what catches storage loss, corruption, a debit landing in
the wrong account, and a payout that never arrived. It cannot catch a rule that is wrong
but applied consistently: if ``plan_record`` computed the wrong fee, the write and this
check would agree on it. That is a different class of defect, and the fixture-level
accounting tests are what cover it.

**Retention is not corruption.** Compaction deliberately folds old entries for resolved
trades into one ``carry_forward`` per account, so their per-trade detail is *supposed* to
be gone. Flagging that would make every long run look corrupt and train an operator to
ignore the check. The carry-forward entries name their own cutoff, so the boundary is
recoverable from the data rather than assumed: a trade whose entries predate the newest
cutoff is reported as compacted and not verified, and one *after* it is held to the full
check. That distinction is stated in the report rather than hidden, because "not verified"
and "verified sound" are not the same claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from event_arb.domain.money import quantize_price
from event_arb.paper.accounts import AccountId
from event_arb.paper.executor import PaperTrade
from event_arb.paper.ledger import EntryKind, LedgerEntry
from event_arb.paper.portfolio import PaperPortfolio

__all__ = ["IntegrityFinding", "IntegrityReport", "verify_trades"]

#: Entry kinds that carry economic movement and are therefore re-derivable from a trade.
#: Reservations are excluded on purpose: a hold and its release net to zero, they are
#: never compacted, and their correctness is a *balance* question that
#: ``AccountReconciliation.holds_are_sane`` already answers.
_ECONOMIC = frozenset({EntryKind.TRADE_DEBIT, EntryKind.FEE_DEBIT, EntryKind.SETTLEMENT_CREDIT})


@dataclass(frozen=True)
class IntegrityFinding:
    """One disagreement between a stored trade and the ledger."""

    code: str
    trade_id: str
    detail: str
    expected: str = ""
    actual: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "trade_id": self.trade_id,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
        }

    def render(self) -> str:
        line = f"    [{self.code}] {self.trade_id[:12]}: {self.detail}"
        if self.expected or self.actual:
            line += f"\n        expected {self.expected or '-'} / found {self.actual or '-'}"
        return line


@dataclass
class IntegrityReport:
    """Whether every retained trade's cash is present, and what could not be checked."""

    findings: list[IntegrityFinding] = field(default_factory=list)
    trades_verified: int = 0
    #: Trades whose entries predate the compaction boundary. Their detail is gone by
    #: design, so they are counted separately rather than being asserted sound.
    trades_compacted: int = 0
    compaction_boundary: datetime | None = None
    entries_checked: int = 0

    @property
    def is_sound(self) -> bool:
        """True when nothing disagreed. Silent about what compaction removed."""
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "is_sound": self.is_sound,
            "trades_verified": self.trades_verified,
            "trades_compacted": self.trades_compacted,
            "entries_checked": self.entries_checked,
            "compaction_boundary": (
                self.compaction_boundary.isoformat() if self.compaction_boundary else None
            ),
            "findings": [f.as_dict() for f in self.findings],
        }

    def render(self) -> str:
        lines = [
            f"  trade/ledger integrity : {'sound' if self.is_sound else 'FAILED'}",
            f"    trades verified      : {self.trades_verified} "
            f"({self.entries_checked} entr(ies) checked)",
        ]
        if self.trades_compacted:
            boundary = (
                self.compaction_boundary.isoformat() if self.compaction_boundary else "unknown"
            )
            lines.append(
                f"    not verified         : {self.trades_compacted} trade(s) compacted "
                f"before {boundary}"
            )
        lines.extend(f.render() for f in self.findings)
        return "\n".join(lines)


def _compaction_boundary(entries: tuple[LedgerEntry, ...]) -> datetime | None:
    """The newest cutoff any carry-forward names, or None when nothing was compacted.

    Read from the entries themselves rather than from configuration, so the boundary a
    report claims is one the stored data can be checked against. Compaction stamps each
    carry-forward's ``created_at`` with the cutoff it applied.
    """
    cutoffs = [e.created_at for e in entries if e.kind is EntryKind.CARRY_FORWARD]
    return max(cutoffs) if cutoffs else None


def _expected_entries(portfolio: PaperPortfolio, trade: PaperTrade) -> dict[str, LedgerEntry]:
    """Every economic entry ``trade`` implies, keyed by entry id.

    Derived from the trade — its fills, its fees, its recorded settlement — and never from
    the ledger being checked. ``plan_record`` covers the fills and fees;
    ``settlement_result`` carries the per-account payouts, which are re-derived here rather
    than by calling ``credit_settlement``, because that method *appends* to the ledger and
    a check must not write to the thing it is inspecting.
    """
    expected = {
        entry.entry_id: entry for entry in portfolio.plan_record(trade) if entry.kind in _ECONOMIC
    }

    result = trade.settlement_result or {}
    payouts = result.get("payouts_by_account")
    if isinstance(payouts, dict):
        for key, amount in payouts.items():
            entry_id = f"{trade.id}:settlement:{key}"
            expected[entry_id] = LedgerEntry(
                entry_id=entry_id,
                account=_parse_account(str(key)),
                kind=EntryKind.SETTLEMENT_CREDIT,
                amount=quantize_price(Decimal(str(amount))),
                trade_id=trade.id,
                detail="expected settlement payout",
            )
    return expected


def _parse_account(key: str) -> AccountId:
    return AccountId.parse(key)


def verify_trades(portfolio: PaperPortfolio) -> IntegrityReport:
    """Check every retained trade's implied cash against the ledger that should hold it."""
    report = IntegrityReport()
    entries = portfolio.ledger.entries
    report.compaction_boundary = _compaction_boundary(entries)

    for trade in portfolio.trades:
        expected = _expected_entries(portfolio, trade)
        actual = {
            entry.entry_id: entry
            for entry in portfolio.ledger.entries_for_trade(trade.id)
            if entry.kind in _ECONOMIC
        }

        # A trade whose entries were legitimately folded into a carry-forward has nothing
        # left to compare. Recognised by the boundary the carry-forward itself names, not
        # by assuming any absence is retention.
        if _is_compacted(expected, actual, report.compaction_boundary):
            report.trades_compacted += 1
            continue

        report.trades_verified += 1
        report.entries_checked += len(expected)

        for entry_id, want in expected.items():
            got = actual.get(entry_id)
            if got is None:
                report.findings.append(
                    IntegrityFinding(
                        code="missing_entry",
                        trade_id=trade.id,
                        detail=f"the ledger has no {want.kind.value} for this trade on "
                        f"{want.account.key}; its cash is unaccounted for",
                        expected=f"{want.account.key} {want.amount}",
                    )
                )
                continue
            if quantize_price(got.amount) != quantize_price(want.amount):
                report.findings.append(
                    IntegrityFinding(
                        code="wrong_amount",
                        trade_id=trade.id,
                        detail=f"the stored {want.kind.value} does not match what the "
                        "trade's own fills imply",
                        expected=str(want.amount),
                        actual=str(got.amount),
                    )
                )
            if got.account != want.account:
                report.findings.append(
                    IntegrityFinding(
                        code="wrong_account",
                        trade_id=trade.id,
                        detail=f"this {want.kind.value} was charged to the wrong account; "
                        "a total would hide it because the aggregate still nets",
                        expected=want.account.key,
                        actual=got.account.key,
                    )
                )
            if got.kind is not want.kind:
                report.findings.append(
                    IntegrityFinding(
                        code="wrong_kind",
                        trade_id=trade.id,
                        detail="the stored entry is of a different kind than the trade implies",
                        expected=want.kind.value,
                        actual=got.kind.value,
                    )
                )

        for entry_id, got in actual.items():
            if entry_id not in expected:
                report.findings.append(
                    IntegrityFinding(
                        code="unexpected_entry",
                        trade_id=trade.id,
                        detail=f"a {got.kind.value} on {got.account.key} that this trade "
                        "does not imply; cash moved for a reason the trade cannot explain",
                        actual=f"{got.account.key} {got.amount}",
                    )
                )

    report.findings.extend(_orphan_findings(portfolio, report.compaction_boundary))
    return report


def _is_compacted(
    expected: dict[str, LedgerEntry],
    actual: dict[str, LedgerEntry],
    boundary: datetime | None,
) -> bool:
    """Whether this trade's absent entries are retention rather than loss.

    Requires all three to hold, so the exemption cannot be borrowed by a trade that simply
    lost its rows: compaction has actually run, this trade has *no* economic entries left
    at all (compaction removes a trade's entries together, never some of them), and it had
    some to lose in the first place.
    """
    return boundary is not None and bool(expected) and not actual


def _orphan_findings(
    portfolio: PaperPortfolio, boundary: datetime | None
) -> list[IntegrityFinding]:
    """Entries charged to a trade that no longer exists.

    The mirror image of a missing entry, and the reason it matters is the same: cash moved
    and nothing retained explains why. Entries older than the compaction boundary are
    exempt — a resolved trade's row can be pruned while a carry-forward carries its cash.
    """
    known = {trade.id for trade in portfolio.trades}
    findings: list[IntegrityFinding] = []
    for entry in portfolio.ledger.entries:
        if entry.kind not in _ECONOMIC or not entry.trade_id or entry.trade_id in known:
            continue
        if boundary is not None and entry.created_at < boundary:
            continue
        findings.append(
            IntegrityFinding(
                code="orphan_entry",
                trade_id=entry.trade_id,
                detail=f"a {entry.kind.value} on {entry.account.key} references a trade "
                "that is not retained; nothing explains this movement",
                actual=f"{entry.account.key} {entry.amount}",
            )
        )
    return findings
