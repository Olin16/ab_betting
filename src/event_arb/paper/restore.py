"""Rebuilding the paper portfolio from storage after a restart.

Startup used to restore markets and pair approvals and nothing else, so every new process
constructed a fresh portfolio at its configured starting balances while the trades that
had spent that money sat on disk. A trial that restarted therefore reported cash it had
already spent and exposure it did not know it held — and the longer the trial, the further
the two drifted.

Two things are restored, from two places, and the separation is deliberate:

**Cash** comes from the ledger, and only from the ledger. Replaying stored entries is
idempotent, so restoring twice — or restoring over a portfolio that already has state —
cannot double-apply a debit.

**History and exposure** come from the trade rows. They are attached without re-applying
any cash, which is what allows retention to prune settled trade rows without a balance
moving. A trade row that cannot be rebuilt is reported rather than skipped silently: an
unreadable row may be an open position, and quietly dropping it would understate exposure
in the one direction that costs money.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from event_arb.domain.models import Opportunity
from event_arb.domain.money import ZERO
from event_arb.observability.logging import get_logger
from event_arb.paper.executor import PaperTrade
from event_arb.paper.ledger import LedgerEntry
from event_arb.paper.portfolio import PaperPortfolio

__all__ = [
    "PortfolioRestoreReport",
    "inspect_portfolio",
    "restore_portfolio",
    "trade_from_row",
]

log = get_logger(__name__)


@dataclass
class PortfolioRestoreReport:
    """What restoring the portfolio recovered, and what it could not."""

    ledger_entries_applied: int = 0
    #: Holds released because the process that took them is gone. See
    #: :meth:`PaperPortfolio.release_stale_holds`.
    stale_holds_released: list[LedgerEntry] = field(default_factory=list)
    trades_restored: int = 0
    trades_unreadable: list[str] = field(default_factory=list)
    open_unhedged_quantity: Decimal = ZERO
    balances: dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        """False when any stored trade could not be rebuilt.

        A trial must not start trading against a portfolio it could only partly read: the
        unreadable rows might be the open positions, so both the balance and the exposure
        limit would be computed from an incomplete picture.
        """
        return not self.trades_unreadable

    def as_dict(self) -> dict[str, object]:
        return {
            "ledger_entries_applied": self.ledger_entries_applied,
            "stale_holds_released": len(self.stale_holds_released),
            "trades_restored": self.trades_restored,
            "trades_unreadable": list(self.trades_unreadable),
            "is_complete": self.is_complete,
            "open_unhedged_quantity": str(self.open_unhedged_quantity),
            "balances": dict(self.balances),
        }


def trade_from_row(row: object) -> PaperTrade:
    """Rebuild one :class:`PaperTrade` from a stored row.

    The opportunity is rebuilt from its own stored snapshot rather than from the trade
    payload, because it is what the legs were priced and accounted against. Raises when
    the row cannot be read — the caller records that rather than inventing a trade.
    """
    snapshot = getattr(row, "opportunity_snapshot", None)
    if not isinstance(snapshot, dict) or not snapshot:
        raise ValueError("row has no opportunity snapshot")
    opportunity = Opportunity.model_validate(snapshot)

    payload = getattr(row, "orders", None)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("row has no trade payload")
    payload = dict(payload)
    # Columns are authoritative over the payload for the fields the schema promoted out of
    # it: the migration backfilled them, and a payload written before that carries neither.
    trade_id = getattr(row, "trade_id", "") or str(payload.get("id", ""))
    if not trade_id:
        raise ValueError("row has no trade id")
    payload["id"] = trade_id
    execution_status = getattr(row, "execution_status", "")
    if execution_status:
        payload["execution_status"] = execution_status
    settled_at = getattr(row, "settled_at", None)
    payload["settled_at"] = settled_at.isoformat() if settled_at is not None else None
    created_at = getattr(row, "created_at", None)
    if created_at is not None:
        payload["created_at"] = created_at.isoformat()
    settlement = getattr(row, "settlement_result", None)
    payload["settlement_result"] = settlement if isinstance(settlement, dict) else None
    return PaperTrade.from_record(payload, opportunity)


def inspect_portfolio(
    portfolio: PaperPortfolio,
    entries: list[LedgerEntry],
    rows: Sequence[object],
) -> PortfolioRestoreReport:
    """Rebuild a portfolio for *reading* — without reaping anything.

    :func:`restore_portfolio` releases holds left by a process that died, which is correct
    when a run is starting: that cash would otherwise stay reserved forever. It is wrong
    for a report. ``event-arb reconcile`` documented itself as read-only and then called
    the restoring version, so inspecting a database mutated it, and inspecting one while a
    trial was running released the holds that trial was *currently using* — the reporting
    command could free cash out from under live execution.

    So the reaping is the one thing this variant omits. Outstanding holds are reported
    exactly as stored, which is also the more useful answer for a report: an operator
    wants to see the stranded reservation, not to have it quietly cleaned up by the act
    of looking at it.
    """
    report = PortfolioRestoreReport()
    report.ledger_entries_applied = portfolio.apply_entries(entries)
    _attach_history(portfolio, rows, report)
    log.info("paper.inspect.completed", **report.as_dict())
    return report


def restore_portfolio(
    portfolio: PaperPortfolio,
    entries: list[LedgerEntry],
    rows: Sequence[object],
) -> PortfolioRestoreReport:
    """Replay stored cash and attach stored history. Idempotent."""
    report = PortfolioRestoreReport()
    report.ledger_entries_applied = portfolio.apply_entries(entries)
    # Before anything reads a balance: a hold that survived into a new process is a hold
    # whose trade will never finish, and reservations are exempt from retention, so an
    # unreaped one reserves cash forever.
    report.stale_holds_released = portfolio.release_stale_holds()
    if report.stale_holds_released:
        log.warning(
            "paper.restore.stale_holds_released",
            count=len(report.stale_holds_released),
            reservations=sorted({e.trade_id for e in report.stale_holds_released}),
            note="cash held by a previous process that did not finish its trade",
        )

    _attach_history(portfolio, rows, report)
    log.info("paper.restore.completed", **report.as_dict())
    return report


def _attach_history(
    portfolio: PaperPortfolio, rows: Sequence[object], report: PortfolioRestoreReport
) -> None:
    """Rebuild trade history onto ``portfolio``, recording rows that could not be read."""
    trades: list[PaperTrade] = []
    for row in rows:
        try:
            trades.append(trade_from_row(row))
        except Exception as exc:
            identifier = str(getattr(row, "trade_id", "") or getattr(row, "id", "?"))
            report.trades_unreadable.append(f"{identifier}: {exc}")
            log.error("paper.restore.trade_unreadable", trade=identifier, error=str(exc))

    report.trades_restored = portfolio.adopt(trades)
    report.open_unhedged_quantity = portfolio.total_unhedged_quantity
    report.balances = dict(portfolio.balances)
