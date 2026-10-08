"""Autonomous trial runtime: ingestion, scanning, and paper execution.

Everything before this module was request-driven — a CLI command or an HTTP call made one
scan happen. That is enough to *observe* prices, but a trial is not about observation. Its
purpose is to accumulate longitudinal evidence about the things that decide whether a
cross-venue strategy actually works: how often the second leg fails, how much of the
detected edge survives real latency, how large unhedged exposure gets, and how many
opportunities are missed while books are stale.

None of that can be collected by hand, so this module runs four cooperating tasks:

``ingest``   keep books current from the venue feeds (WebSocket where available, REST
             refresh on an interval, periodic resync where continuity cannot be proven)
``scan``     detect opportunities on an interval and persist every one, plus the
             rejection reasons that explain the gaps
``execute``  paper-trade what the scan found, with real latency between the legs
``maintain`` prune snapshots to the retention window and re-verify rules

Live trading is not reachable from here. The runner drives ``Scanner.paper_trade``, and
the only execution adapter in the project refuses every submission unconditionally.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from event_arb.alerts import AlertDispatcher
from event_arb.config import Settings
from event_arb.domain.models import Opportunity
from event_arb.observability.logging import get_logger
from event_arb.orderbooks.synchronizer import BookSynchronizer
from event_arb.paper.durability import TradeNotDurableError
from event_arb.scanner import Scanner

__all__ = ["TrialRunner", "TrialStats"]

log = get_logger(__name__)

#: How often to prune snapshots and re-check rules integrity. Retention work is cheap to
#: defer; noticing that ingestion has died is not, which is why health checks no longer
#: share this interval. See :data:`TrialSettings.health_check_interval_seconds`.
MAINTENANCE_INTERVAL_SECONDS = 300.0


@dataclass
class TrialStats:
    """Counters describing what the trial has actually done."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    scans: int = 0
    opportunities_detected: int = 0
    confirmed_detected: int = 0
    paper_trades: int = 0
    refreshes: int = 0
    #: Refresh batches that returned but left a required market without a usable book.
    #: Any value above zero means some of this run's scans did not happen because the
    #: feed was not delivering, which a flat detected-opportunity count cannot show.
    refreshes_incomplete: int = 0
    snapshots_pruned: int = 0
    #: Detections that were observed but not re-announced inside the cooldown window.
    alerts_suppressed: int = 0
    errors: int = 0
    health_alerts: int = 0
    #: Trades the capital gate refused. A rising count is not a quiet market.
    paper_trades_refused: int = 0
    #: Attempts whose accounting storage refused. Any value above zero means this run's
    #: figures cannot be fully reproduced from storage; see
    #: :mod:`event_arb.paper.durability`.
    paper_trades_invalidated: int = 0
    #: Cycles skipped because the watchdog had paused trading.
    trading_paused_cycles: int = 0
    trading_paused: bool = False
    paused_reason: str | None = None
    stopped_reason: str | None = None
    #: The worst per-loop streak, which is what the pause and stop thresholds compare
    #: against. Kept as a plain int because it is what operators and the dashboard read.
    consecutive_failures: int = 0
    #: The streak per loop, because a loop's failures are only cleared by *its own*
    #: success. One shared counter let a healthy refresh erase a failing scan's history.
    consecutive_failures_by_loop: dict[str, int] = field(default_factory=dict)
    last_scan_at: datetime | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": int((datetime.now(UTC) - self.started_at).total_seconds()),
            "scans": self.scans,
            "opportunities_detected": self.opportunities_detected,
            "confirmed_detected": self.confirmed_detected,
            "paper_trades": self.paper_trades,
            "refreshes": self.refreshes,
            "refreshes_incomplete": self.refreshes_incomplete,
            "feed_was_healthy": self.refreshes_incomplete == 0,
            "snapshots_pruned": self.snapshots_pruned,
            "alerts_suppressed": self.alerts_suppressed,
            "errors": self.errors,
            "health_alerts": self.health_alerts,
            "paper_trades_refused": self.paper_trades_refused,
            "paper_trades_invalidated": self.paper_trades_invalidated,
            "accounting_is_reproducible": self.paper_trades_invalidated == 0,
            "trading_paused": self.trading_paused,
            "trading_paused_cycles": self.trading_paused_cycles,
            "paused_reason": self.paused_reason,
            "stopped_reason": self.stopped_reason,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_failures_by_loop": dict(self.consecutive_failures_by_loop),
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "last_error": self.last_error,
        }


class TrialRunner:
    """Owns the background tasks that turn the scanner into a running trial."""

    def __init__(
        self,
        scanner: Scanner,
        settings: Settings,
        alerts: AlertDispatcher | None = None,
        use_websockets: bool = True,
    ) -> None:
        self._scanner = scanner
        self._settings = settings
        self._alerts = alerts
        self._use_websockets = use_websockets
        self._synchronizer = BookSynchronizer(
            scanner.store,
            scanner.adapters,
            polymarket_product=scanner.polymarket_product,
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._budget_warned = False
        self._pause_reasons: dict[str, str] = {}
        #: Set when a cooperating task ends unexpectedly. Separate from a watchdog stop
        #: because the two need different explanations in the summary.
        self.task_failed = False
        self.stats = TrialStats()

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not self._stopping.is_set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Prime books, then launch ingestion, scanning, execution and maintenance."""
        subscriptions = self._scanner.watched_markets()
        if not any(subscriptions.values()):
            log.warning(
                "trial.no_watched_markets",
                note="nothing to ingest; configure trial.watched_pairs or approve a pair",
            )

        warning = self._settings.validate_freshness_budget()
        if warning:
            log.warning("trial.freshness_budget", detail=warning)

        await self._synchronizer.prime(subscriptions)

        if self._use_websockets:
            # WebSocket ingestion; the synchronizer also schedules REST resync for
            # Polymarket, which cannot prove stream continuity.
            await self._synchronizer.start(subscriptions)
        self._scanner.streaming = True

        self._tasks = [
            asyncio.create_task(self._cycle_loop(), name="trial-cycle"),
            asyncio.create_task(self._maintenance_loop(), name="trial-maintenance"),
            # Its own loop, at its own cadence. Health used to be checked inside the
            # 300 s maintenance loop, so a feed that died was noticed up to five minutes
            # later — against a stop condition operations documents in minutes.
            asyncio.create_task(self._health_loop(), name="trial-health"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._task_finished)
        log.info(
            "trial.started",
            watched_markets={v.value: len(ids) for v, ids in subscriptions.items()},
            websockets=self._use_websockets,
            scan_interval_s=self._settings.trial.scan_interval_seconds,
            auto_paper_trade=self._settings.trial.trades_automatically,
            observation_only=self._settings.trial.observation_only,
        )
        if self._settings.trial.observation_only:
            log.warning(
                "trial.observation_only",
                note="explicitly observing; no paper trade will be executed this run",
            )

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        """Wake the whole trial when a cooperating task dies.

        The loops catch and record their own exceptions, so a task that ends at all ended
        for a reason no loop expected. Without this the remaining tasks kept running and
        the CLI kept waiting: the trial carried on with, say, no scanning at all, and
        still exited zero with a summary that looked like a completed run.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.stats.errors += 1
        self.stats.last_error = f"{task.get_name()}: {exc}"
        self.stats.stopped_reason = f"{task.get_name()} task died: {exc}"
        self.task_failed = True
        log.error("trial.task_died", task=task.get_name(), error=str(exc))
        self._stopping.set()

    async def wait_until_stopped(self) -> None:
        """Block until the trial stops itself — the watchdog gave up, or a task died.

        The CLI used to wait on an ``asyncio.Event()`` nothing ever set, so a watchdog
        stop left the process sitting there until an operator noticed.

        Bounding the wait is the caller's job, with ``asyncio.timeout``, so that a run
        with a duration and a run without one are the same code path::

            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(duration):   # None means no deadline
                    await runner.wait_until_stopped()
            if runner.stopped: ...

        Read :attr:`stopped` afterwards to tell "it stopped itself" from "the clock ran
        out", because those are the two outcomes an unattended run must not conflate.
        """
        await self._stopping.wait()

    @property
    def stopped(self) -> bool:
        """True once the runner has stopped itself, whatever the caller was waiting for."""
        return self._stopping.is_set()

    @property
    def failed(self) -> bool:
        """True when the trial ended for a reason that should fail the process."""
        return self.task_failed or self.stats.stopped_reason is not None

    async def stop(self) -> None:
        """Stop every task and wait for it, so shutdown leaves nothing running."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        await self._synchronizer.stop()
        self._scanner.streaming = False
        log.info("trial.stopped", **self.stats.as_dict())

    async def _sleep(self, seconds: float) -> bool:
        """Wait, returning False if a stop was requested meanwhile."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        return not self._stopping.is_set()

    # ---------------------------------------------------------------------- loops

    async def _cycle_loop(self) -> None:
        """Refresh, persist, then scan — in that order, in one cycle.

        Refresh and scan used to be independent tasks on independent timers, which meant
        a scan could land on the previous cycle's snapshot while the next REST request was
        still in flight. With a 5 s refresh interval and a 2 s freshness bound, that book
        was already stale, so the scan either rejected everything or (worse, had the bound
        been loosened) priced an old book as current.

        Sequencing them removes the race outright rather than papering over it by widening
        the staleness threshold, which would trade a visible failure for an invisible one.
        The REST refresh remains the backstop that repairs a book after a gap, a malformed
        update, or silent divergence on a feed with no sequence numbers.
        """
        interval = max(
            self._settings.trial.refresh_interval_seconds,
            self._settings.trial.scan_interval_seconds,
        )
        while await self._sleep(interval):
            cycle_started = time.monotonic()
            try:
                outcome = await self._scanner.refresh_books()
                await self._scanner.record_books(outcome.books)
                self.stats.refreshes += 1
                # Re-check the budget against what refreshing actually cost.
                measured = self._scanner.refresh_timing.worst_batch_seconds
                budget_warning = self._settings.validate_freshness_budget(measured)
                if budget_warning and not self._budget_warned:
                    self._budget_warned = True
                    log.warning(
                        "trial.freshness_budget_measured",
                        detail=budget_warning,
                        **self._scanner.refresh_timing.as_dict(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("refresh", exc)
                # A failed refresh means the books are unverified. Scanning them would
                # report on state we could not confirm, so skip to the next cycle.
                await self._note_failure("refresh")
                continue

            if not outcome.is_complete:
                # The batch returned, but not with a usable book for every market this
                # trial is required to price. That is an ingestion failure however
                # politely the adapters reported it, so it advances the same streak an
                # exception would and skips the scan that would otherwise record a
                # confident "no opportunities" against books we do not have.
                self.stats.refreshes_incomplete += 1
                self.stats.last_error = f"incomplete book coverage ({outcome.reason()})"
                log.error(
                    "trial.refresh_incomplete",
                    note="required markets have no usable book; this is an outage, "
                    "not a quiet market",
                    **outcome.as_dict(),
                )
                await self._note_failure("refresh")
                continue

            self._note_success("refresh")
            try:
                # Measured, not assumed: this is how old the first book in the batch
                # actually is when the scan reads it, including the persist step above.
                self._scanner.refresh_timing.record_scan_lag(time.monotonic() - cycle_started)
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("scan", exc)
                await self._note_failure("scan")
            else:
                self._note_success("scan")

    async def _scan_once(self) -> None:
        result = await self._scanner.scan_and_persist()
        self.stats.scans += 1
        self.stats.last_scan_at = result.scanned_at
        # Count what was observed, not what was announced. The cooldown governs alerting;
        # a stat that tracked it would drift from the persisted evidence over a long run.
        self.stats.opportunities_detected += len(result.all_detected)
        self.stats.confirmed_detected += len(result.confirmed)
        self.stats.alerts_suppressed += len(result.alert_suppressed)

        for opportunity in result.opportunities:
            await self._alert(opportunity)
            if self._should_paper_trade(opportunity):
                await self._paper_trade(opportunity)

    async def _alert(self, opportunity: Opportunity) -> None:
        """Announce an opportunity. Never let announcing it stop the trial.

        AlertDispatcher already swallows httpx errors, but only those. Anything else — a
        payload it cannot build, a misconfigured URL — escaped to the cycle loop, which
        abandoned the rest of the batch. A flaky webhook could therefore stop paper trading,
        silently converting a notification problem into a hole in the evidence.
        """
        if self._alerts is None:
            return
        try:
            await self._alerts.dispatch(opportunity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error("alert", exc)

    def _should_paper_trade(self, opportunity: Opportunity) -> bool:
        trial = self._settings.trial
        if not trial.trades_automatically:
            return False
        if self.stats.trading_paused:
            # Paused, not stopped: ingestion and detection continue, so the evidence gap
            # is recorded rather than being an unexplained absence of trades.
            self.stats.trading_paused_cycles += 1
            return False
        if opportunity.is_confirmed:
            return True
        # Research candidates are unapproved pairs. Simulating them yields execution
        # evidence without ever implying the pair is settlement-equivalent, but it is
        # off by default so trial results are not diluted by unverified pairs.
        return trial.paper_trade_research_candidates

    async def _paper_trade(self, opportunity: Opportunity) -> None:
        try:
            # The funded path, never the bare one: automated execution must pass the
            # capital gate, or the trial accumulates exposure no operator would take and
            # spends money no account holds.
            trade = await self._scanner.paper_trade_if_funded(opportunity)
        except asyncio.CancelledError:
            raise
        except TradeNotDurableError as exc:
            # Storage would not take the attempt, so it was never published. Counted
            # separately from an execution error because the trial's evidence, not just
            # this cycle, is what it damages — and routed to `_note_failure` all the same,
            # which escalates straight to a stop: a run that cannot write its own
            # accounting has nothing left to observe.
            self.stats.paper_trades_invalidated += 1
            self._record_error("paper_trade", exc)
            await self._note_failure("paper_trade")
            return
        except Exception as exc:
            self._record_error("paper_trade", exc)
            await self._note_failure("paper_trade")
            return
        # A refusal is the gate working, not an execution failure, so it clears the streak
        # exactly as a fill does. Counting refusals as failures would stop a trial for
        # running out of simulated money, which is a result rather than a fault.
        self._note_success("paper_trade")
        if trade is None:
            self.stats.paper_trades_refused += 1
            return
        self.stats.paper_trades += 1
        log.info(
            "trial.paper_trade",
            trade_id=trade.id,
            status=trade.status.value,
            hedged=str(trade.hedged_quantity),
            unhedged=str(trade.unhedged_quantity),
            pnl=str(trade.realized_pnl_if_hedged),
        )

    def _check_health(self) -> list[str]:
        """Report adapters that have gone quiet.

        An unattended trial that silently stops receiving data still produces a tidy
        report — of nothing. Degraded ingestion has to be loud, or the absence of
        opportunities gets mistaken for an absence of opportunity.
        """
        report = self._scanner.health()
        return [
            str(entry.get("venue")) for entry in report.adapters if not entry.get("fresh", False)
        ]

    async def _health_loop(self) -> None:
        """Watch ingestion at its own cadence, and act rather than only logging.

        A degraded feed used to produce one log line every five minutes and nothing else,
        so an unattended trial kept paper-trading against books nothing was refreshing.
        Sustained degradation now pauses trading and, if it persists, stops the run —
        because every further cycle spends rate-limit budget to record evidence that is
        already known to be unreliable.
        """
        interval = self._settings.trial.health_check_interval_seconds
        while await self._sleep(interval):
            try:
                unhealthy = self._check_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Bounded, not merely logged. A health check that keeps raising is a
                # watchdog that is not watching, and looping on it forever left the trial
                # with no supervision at all while still reporting itself supervised.
                self._record_error("health", exc)
                await self._note_failure(self.HEALTH_LOOP)
                continue
            if unhealthy:
                self.stats.health_alerts += 1
                log.error(
                    "trial.adapter_unhealthy",
                    venues=unhealthy,
                    note="ingestion is degraded; detected opportunity counts are "
                    "not meaningful while this persists",
                )
                await self._pause(
                    self.PAUSE_HEALTH, f"degraded ingestion on {', '.join(unhealthy)}"
                )
                # Pausing alone was the whole response, despite this loop's docstring
                # promising a stop "if it persists". A permanently dead feed therefore
                # sat paused until the clock ran out and exited zero. Routing the streak
                # through the same watchdog every other loop uses makes the promise real.
                await self._note_failure(self.HEALTH_LOOP)
            else:
                self._resume(self.PAUSE_HEALTH)
                self._note_success(self.HEALTH_LOOP)

    async def _maintenance_loop(self) -> None:
        while await self._sleep(MAINTENANCE_INTERVAL_SECONDS):
            try:
                self.stats.snapshots_pruned += await self._scanner.prune_snapshots()
                # Re-fetch metadata so a mid-trial rules change is noticed promptly
                # rather than only at the next restart.
                await self._scanner.discover()
                await self._scanner.persist_markets()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("maintenance", exc)

    # ------------------------------------------------------------------- watchdog
    #
    # Trading can be paused for two independent reasons, so they are tracked as two.
    # Collapsing them into one flag meant a healthy adapter check cleared a pause that a
    # failing refresh loop had raised — the trial resumed trading against books it had
    # just failed to fetch, which is the exact situation the pause exists for.

    #: Pause causes, keyed by source. Trading runs only while this is empty.
    PAUSE_FAILURES = "failures"
    PAUSE_HEALTH = "health"

    #: The health loop's own failure streak. Named separately from :data:`PAUSE_HEALTH`
    #: because the pause it raises and the streak that escalates to a stop are two
    #: different things: the pause lifts the moment the feed recovers, while the streak
    #: is what decides the run is not worth continuing.
    HEALTH_LOOP = "health"

    #: Loops whose failure means the *books* cannot be trusted, so trading pauses while
    #: ingestion and detection carry on and the gap gets recorded.
    #:
    #: Execution is deliberately absent. Pausing execution because execution failed stops
    #: the only thing that could advance its failure streak, so the count freezes one
    #: short of the stop threshold and the trial sits paused indefinitely — detecting
    #: opportunities it will never attempt again, and never reaching a verdict. Repeated
    #: execution failures therefore escalate straight to a stop.
    PAUSING_LOOPS = frozenset({"refresh", "scan"})

    @staticmethod
    def _failure_source(what: str) -> str:
        """The pause source for one loop's failures, so loops cannot clear each other."""
        return f"{TrialRunner.PAUSE_FAILURES}:{what}"

    async def _note_failure(self, what: str) -> None:
        """Count a failure against ``what``, pausing and eventually stopping.

        The count is per loop. A single shared counter was reset by any success, and the
        cycle refreshes before it scans, so one failed scan per successful refresh never
        accumulated past one: a trial whose scanning or paper execution was failing every
        single cycle sailed past both thresholds and reported itself healthy. Failures are
        only forgiven by a success of the *same* kind.
        """
        trial = self._settings.trial
        count = self.stats.consecutive_failures_by_loop.get(what, 0) + 1
        self.stats.consecutive_failures_by_loop[what] = count
        self.stats.consecutive_failures = max(self.stats.consecutive_failures_by_loop.values())
        if count >= trial.stop_after_consecutive_failures:
            await self._stop_trading(f"{count} consecutive failures ({what})", count)
            return
        if count >= trial.pause_after_consecutive_failures and what in self.PAUSING_LOOPS:
            await self._pause(self._failure_source(what), f"{count} consecutive failures ({what})")

    def _note_success(self, what: str) -> None:
        """A success clears that loop's streak and its pause, and only that loop's."""
        if self.stats.consecutive_failures_by_loop.pop(what, 0):
            self.stats.consecutive_failures = max(
                self.stats.consecutive_failures_by_loop.values(), default=0
            )
        self._resume(self._failure_source(what))

    async def _stop_trading(self, reason: str, count: int) -> None:
        """Give up on the trial. Nothing may stand between deciding this and doing it.

        The stop event used to be set *after* an audit write, so when storage was the
        failing subsystem the watchdog raised on the way to stopping and the run continued
        with ``stopped_reason`` set — reporting itself stopped while still spending
        rate-limit budget. A failure handler cannot depend on the thing that failed.
        """
        self.stats.stopped_reason = reason
        self._stopping.set()
        log.error(
            "trial.watchdog_stop",
            reason=reason,
            note="stopping the trial; it has not been collecting usable evidence",
        )
        await self._audit_decision("trial.watchdog_stop", {"reason": reason, "failures": count})

    async def _pause(self, source: str, reason: str) -> None:
        if source in self._pause_reasons:
            return
        self._pause_reasons[source] = reason
        self.stats.trading_paused = True
        self.stats.paused_reason = "; ".join(sorted(self._pause_reasons.values()))
        log.error(
            "trial.watchdog_pause",
            source=source,
            reason=reason,
            note="paper trading paused; ingestion and detection continue so the gap "
            "is recorded rather than looking like a quiet market",
        )
        await self._audit_decision("trial.watchdog_pause", {"source": source, "reason": reason})

    async def _audit_decision(self, action: str, detail: dict[str, object]) -> None:
        """Write the watchdog's decision to the audit log, best effort.

        Recording a decision must never be able to change it. The pause and stop paths both
        reach storage, and storage is one of the things whose failure gets them called.
        """
        try:
            await self._scanner.audit("system", action, "", detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Counted, not routed back through _note_failure: a watchdog that escalates on
            # its own bookkeeping failures would stop trials for the wrong reason.
            self.stats.errors += 1
            log.error(
                "trial.watchdog_audit_unrecorded",
                action=action,
                error=str(exc),
                note="the watchdog decision stands; only its audit row is missing",
            )

    def _resume(self, source: str) -> None:
        if self._pause_reasons.pop(source, None) is None:
            return
        self.stats.trading_paused = bool(self._pause_reasons)
        self.stats.paused_reason = (
            "; ".join(sorted(self._pause_reasons.values())) if self._pause_reasons else None
        )
        log.info("trial.watchdog_resume", source=source, still_paused=self.stats.trading_paused)

    def _record_error(self, where: str, exc: Exception) -> None:
        """A failing loop must not kill the trial; record it and keep going."""
        self.stats.errors += 1
        self.stats.last_error = f"{where}: {exc}"
        log.error("trial.loop_error", loop=where, error=str(exc))

    # --------------------------------------------------------------------- status

    def status(self) -> dict[str, object]:
        health = self._scanner.health()
        return {
            "running": self.running,
            "streaming": self._scanner.streaming,
            "stats": self.stats.as_dict(),
            "adapters": health.adapters,
            "books_tracked": len(self._scanner.store),
            "watched_markets": {
                venue.value: ids for venue, ids in self._scanner.watched_markets().items()
            },
            "live_trading_possible": False,
            "observation_only": self._settings.trial.observation_only,
            "trading_paused": self.stats.trading_paused,
            "paper_accounts": self._scanner.portfolio.ledger.as_dict(),
            "open_unhedged_quantity": str(self._scanner.portfolio.total_unhedged_quantity),
        }
