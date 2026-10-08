"""What happens to a simulated attempt whose accounting could not be stored.

The rule this module exists to enforce: **nothing is published to memory before storage
has accepted it.** A paper portfolio is only meaningful because it can be reconstructed;
a balance that exists solely in a live process is not a simulated position, it is a
number that disappears at the next restart while the report still calls the run clean.

The defect that motivated it: ``paper_trade`` debited cash and appended the trade to the
portfolio *before* the transaction that stored them. With the trade write failing, memory
held one trade and balances of ``kalshi:USD=9951.2500`` / ``polymarket_us:USD=9960.5900``;
reopening the same database showed zero trades and ``10000.0000`` in both accounts, and
the restore report called that complete. The loss was silent in both directions — the run
kept trading against balances no restart could reproduce.

Publishing only after a commit makes memory and storage agree by construction, and turns
a failed write into something with a name: the attempt is **invalidated**. Invalidation is
not the same as "did not happen". In a *paper* trial no venue holds the other side, so an
unstored simulated fill genuinely has no consequence anywhere — but the run has still lost
an observation it was counting, and a trial that resumes as though it had not is reporting
evidence it does not have. So an invalidated attempt is recorded, counted, excluded from
hedge statistics, and surfaced in the reconciliation as a stated gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from event_arb.domain.money import ZERO

__all__ = ["DurabilityStage", "InvalidatedAttempt", "TradeNotDurableError"]


class DurabilityStage(StrEnum):
    """Where in an attempt the write that failed was."""

    #: The leg-1 checkpoint, written before leg 2 is attempted. Its failure aborts the
    #: attempt rather than proceeding into exposure nothing could carry across a restart.
    CHECKPOINT = "leg1_checkpoint"
    #: The completed trade and its cash entries.
    TRADE = "completed_trade"
    #: An attempt already interrupted after leg 1, whose exposure could not be stored.
    INTERRUPTED = "interrupted_attempt"
    #: A settlement credit and its updated trade row.
    SETTLEMENT = "settlement"


@dataclass(frozen=True)
class InvalidatedAttempt:
    """One simulated attempt that storage refused, and what it therefore does not show.

    Kept as data rather than only a log line because the trial's acceptance criteria read
    it: a run with invalidated attempts has not produced complete evidence, however clean
    the trades that did land look.
    """

    trade_id: str
    pair_id: str
    stage: DurabilityStage
    execution_status: str
    unhedged_quantity: Decimal = ZERO
    error: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def consequence(self) -> str:
        """What this invalidation means for the figures the run is reporting.

        The two shapes differ and the difference matters to an operator. An attempt
        refused before publication is absent from every count: the run under-reports. A
        settlement whose write failed *is* in the counts but is not in storage: the run
        over-reports until the write is repeated, and a restart before then reverts it.
        """
        if self.stage is DurabilityStage.SETTLEMENT:
            return (
                "credited in memory but not in storage; re-run the settlement command, "
                "and expect a restart before then to show the trade unsettled"
            )
        return (
            "never published to the portfolio; absent from every balance, count and "
            "evidence grade the run reports"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "trade_id": self.trade_id,
            "pair_id": self.pair_id,
            "stage": self.stage.value,
            "execution_status": self.execution_status,
            "unhedged_quantity": str(self.unhedged_quantity),
            "error": self.error,
            "consequence": self.consequence,
            "at": self.at.isoformat(),
        }


class TradeNotDurableError(Exception):
    """Storage would not accept a simulated attempt, so it was not published.

    Raised *instead of* returning a trade, so no caller can mistake an unstored attempt
    for one that happened. Carries the invalidation record for the caller to report.
    """

    def __init__(self, attempt: InvalidatedAttempt, cause: BaseException) -> None:
        super().__init__(
            f"paper attempt {attempt.trade_id} was not made durable at "
            f"{attempt.stage.value}: {cause!r}"
        )
        self.attempt = attempt
        self.cause = cause
