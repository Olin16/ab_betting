"""An append-only record of what a run did, kept *outside* the database it is about.

Invalidation used to live in a Python list. ``Scanner._invalidate`` appended to
``state.invalidated_attempts``, incremented a metric, and stopped there — so the one fact
a failed run most needed to carry forward was the one fact that died with the process.
Before closing, a run that had lost an attempt reported ``invalidated=1`` and
``is_complete_evidence=False``; reopening the very same database reported ``invalidated=0``
and ``is_complete_evidence=True``. A restart silently upgraded incomplete evidence to
complete, which is worse than losing the attempt: the report stopped saying so.

The obvious repair — write the invalidation to the database — does not work, and the
reason is the whole design constraint here. An attempt is invalidated *because the
database refused to take it*. A failure record that depends on the failed subsystem is not
a failure record; it is the same silence with more steps. So the journal is a plain
append-only file beside the database, written with ``fsync`` and never rewritten in place.

It carries two kinds of fact a fresh process cannot otherwise learn:

**Invalidated attempts.** Recorded at the moment storage refuses, so the next run knows
the previous one lost an observation it was counting.

**Unfinished runs.** A run appends ``run_started`` when it begins and ``run_finished``
when it shuts down cleanly. A start with no finish means the process died without
stopping — a kill, a host going away, a crash — and *nothing else in the system records
that*. The database looks identical either way, which is exactly how an abrupt death used
to pass as a clean run.

Both fail **closed**: a database carrying either one cannot produce complete evidence
until an operator says otherwise. Saying otherwise is itself an append
(``run_acknowledged``), not a deletion — the history of a failed run is never erased, it
is annotated. An operator who acknowledges a crash is making an auditable claim that they
looked; the journal keeps both the crash and the claim.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from event_arb.domain.money import ZERO
from event_arb.observability.logging import get_logger
from event_arb.paper.durability import DurabilityStage, InvalidatedAttempt

__all__ = [
    "JournalHistory",
    "RunJournal",
    "UnfinishedRun",
    "journal_path_for",
]

log = get_logger(__name__)

#: Record kinds. Written as strings so a human reading the file needs no schema.
RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
RUN_ACKNOWLEDGED = "run_acknowledged"
ATTEMPT_INVALIDATED = "attempt_invalidated"
ATTEMPT_RESOLVED = "attempt_resolved"


def journal_path_for(database_url: str) -> Path | None:
    """The journal that belongs to one database, or None when there is no file to sit beside.

    Derived from the URL rather than configured separately, so the journal travels with
    the trial database it describes and an operator cannot point a run at one database
    while its failure history accumulates next to another. An in-memory or non-SQLite URL
    gets no journal: there is no restart for it to survive.
    """
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw = database_url[len(prefix) :]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw).with_suffix(Path(raw).suffix + ".journal")


@dataclass(frozen=True)
class UnfinishedRun:
    """A run that started and never recorded a clean shutdown."""

    run_id: str
    started_at: str
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"run_id": self.run_id, "started_at": self.started_at, "note": self.note}


@dataclass
class JournalHistory:
    """What the journal says about every run that has touched this database."""

    invalidated: list[InvalidatedAttempt] = field(default_factory=list)
    unfinished: list[UnfinishedRun] = field(default_factory=list)
    #: Trade ids whose earlier invalidation a later write repaired. Kept for reporting;
    #: the repaired attempts are already excluded from :attr:`invalidated`.
    resolved: list[str] = field(default_factory=list)
    runs_recorded: int = 0
    #: Lines that could not be parsed. Reported rather than skipped: a journal we cannot
    #: fully read is itself a reason to distrust the evidence, and silently ignoring the
    #: damaged lines would be the same failure this module exists to stop.
    unreadable_lines: int = 0

    @property
    def is_clean(self) -> bool:
        """True only when no prior run lost an attempt, crashed, or wrote a bad line."""
        return not self.invalidated and not self.unfinished and not self.unreadable_lines

    def summary(self) -> str:
        """One line an operator can act on. Empty when there is nothing to say."""
        parts = []
        if self.invalidated:
            parts.append(f"{len(self.invalidated)} attempt(s) storage refused in earlier run(s)")
        if self.unfinished:
            ids = ", ".join(r.run_id for r in self.unfinished)
            parts.append(f"{len(self.unfinished)} run(s) ended without a clean shutdown: {ids}")
        if self.unreadable_lines:
            parts.append(f"{self.unreadable_lines} unreadable journal line(s)")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "runs_recorded": self.runs_recorded,
            "resolved_attempts": list(self.resolved),
            "invalidated_attempts": [a.as_dict() for a in self.invalidated],
            "unfinished_runs": [r.as_dict() for r in self.unfinished],
            "unreadable_lines": self.unreadable_lines,
            "is_clean": self.is_clean,
            "summary": self.summary(),
        }


class RunJournal:
    """Append-only, fsync'd sidecar. Every write is one line; no line is ever rewritten."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.run_id = uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------- writing

    def _append(self, kind: str, **payload: object) -> bool:
        """Append one record, returning whether it reached the disk.

        Never raises. The callers are failure handlers — the trade write already failed,
        or the process is shutting down — and a failure handler that can itself raise
        turns one lost attempt into a lost run. A journal write that fails is logged at
        error level and reported as unwritten, which is the most this layer can honestly
        do when both the database and its sidecar are refusing.
        """
        record = {
            "kind": kind,
            "run_id": self.run_id,
            "at": datetime.now(UTC).isoformat(),
            **payload,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                # The point of the journal is surviving the thing that killed the process.
                # Buffered in the OS page cache is not surviving it.
                os.fsync(handle.fileno())
        except OSError as exc:
            log.error(
                "paper.journal.write_failed",
                kind=kind,
                path=str(self.path),
                error=str(exc),
                note="this run's failure history is not durable; treat its evidence as "
                "incomplete regardless of what the report says",
            )
            return False
        return True

    def run_started(self, **detail: object) -> bool:
        """Mark this run open. Only a clean shutdown closes it."""
        return self._append(RUN_STARTED, detail=detail)

    def run_finished(self, **detail: object) -> bool:
        return self._append(RUN_FINISHED, detail=detail)

    def attempt_invalidated(self, attempt: InvalidatedAttempt) -> bool:
        return self._append(ATTEMPT_INVALIDATED, attempt=attempt.as_dict())

    def attempt_resolved(self, trade_id: str, *, note: str) -> bool:
        """Record that a previously refused attempt was later stored after all.

        A retry that succeeds is not a permanent hole, and leaving the invalidation
        standing would make every repaired run look broken forever. But the repair is
        written as its *own* record rather than by removing the failure: the file keeps
        both, so the history still shows that the write failed once and what fixed it.
        """
        return self._append(ATTEMPT_RESOLVED, resolved_trade=trade_id, note=note)

    def acknowledge(self, run_id: str, *, by: str, note: str) -> bool:
        """Annotate an earlier run as reviewed. Appends; never edits or removes.

        The crash stays in the file forever. What this adds is the operator's statement
        that they looked at it, so the next start can stop blocking without anyone having
        to delete the evidence to make the gate go green.
        """
        return self._append(RUN_ACKNOWLEDGED, acknowledged_run=run_id, by=by, note=note)

    # ------------------------------------------------------------------- reading

    @classmethod
    def read(cls, path: Path | None) -> JournalHistory:
        """Replay the file into a history. A missing journal is an empty one, not an error."""
        history = JournalHistory()
        if path is None or not path.exists():
            return history

        started: dict[str, UnfinishedRun] = {}
        finished: set[str] = set()
        acknowledged: set[str] = set()
        resolved: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.error("paper.journal.read_failed", path=str(path), error=str(exc))
            history.unreadable_lines += 1
            return history

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                kind = record["kind"]
                run_id = record["run_id"]
            except (ValueError, KeyError, TypeError):
                history.unreadable_lines += 1
                continue

            if kind == RUN_STARTED:
                history.runs_recorded += 1
                started[run_id] = UnfinishedRun(run_id=run_id, started_at=record.get("at", ""))
            elif kind == RUN_FINISHED:
                finished.add(run_id)
            elif kind == RUN_ACKNOWLEDGED:
                acknowledged.add(str(record.get("acknowledged_run", "")))
            elif kind == ATTEMPT_RESOLVED:
                resolved.add(str(record.get("resolved_trade", "")))
            elif kind == ATTEMPT_INVALIDATED:
                attempt = _attempt_from(record.get("attempt"))
                if attempt is None:
                    history.unreadable_lines += 1
                else:
                    history.invalidated.append(attempt)

        # Resolutions are applied after the whole file is read, so a repair recorded in a
        # later run still clears a failure recorded in an earlier one.
        history.resolved = sorted(resolved)
        history.invalidated = [a for a in history.invalidated if a.trade_id not in resolved]
        history.unfinished = [
            run
            for run_id, run in started.items()
            if run_id not in finished and run_id not in acknowledged
        ]
        return history


def _attempt_from(payload: object) -> InvalidatedAttempt | None:
    """Rebuild one invalidation, or None when the line cannot be trusted.

    Returning None rather than a partly-filled record keeps a damaged line countable as
    damage. An attempt reconstructed from missing fields would read as evidence.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return InvalidatedAttempt(
            trade_id=str(payload["trade_id"]),
            pair_id=str(payload["pair_id"]),
            stage=DurabilityStage(payload["stage"]),
            execution_status=str(payload["execution_status"]),
            unhedged_quantity=_decimal(payload.get("unhedged_quantity")),
            error=str(payload.get("error", "")),
            at=datetime.fromisoformat(str(payload["at"])),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO
