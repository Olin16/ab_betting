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
from dataclasses import dataclass, field
from datetime import UTC, datetime

from event_arb.alerts import AlertDispatcher
from event_arb.config import Settings
from event_arb.domain.models import Opportunity
from event_arb.observability.logging import get_logger
from event_arb.orderbooks.synchronizer import BookSynchronizer
from event_arb.scanner import Scanner

__all__ = ["TrialRunner", "TrialStats"]

log = get_logger(__name__)

#: How often to prune snapshots and re-check rules integrity.
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
    snapshots_pruned: int = 0
    errors: int = 0
    health_alerts: int = 0
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
            "snapshots_pruned": self.snapshots_pruned,
            "errors": self.errors,
            "health_alerts": self.health_alerts,
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
        self._synchronizer = BookSynchronizer(scanner.store, scanner.adapters)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._budget_warned = False
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
        ]
        log.info(
            "trial.started",
            watched_markets={v.value: len(ids) for v, ids in subscriptions.items()},
            websockets=self._use_websockets,
            scan_interval_s=self._settings.trial.scan_interval_seconds,
            auto_paper_trade=self._settings.trial.auto_paper_trade,
        )

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
            try:
                books = await self._scanner.refresh_books()
                await self._scanner.record_books(books)
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
                continue

            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("scan", exc)

    async def _scan_once(self) -> None:
        result = await self._scanner.scan_and_persist()
        self.stats.scans += 1
        self.stats.last_scan_at = result.scanned_at
        self.stats.opportunities_detected += len(result.opportunities)
        self.stats.confirmed_detected += len(result.confirmed)

        for opportunity in result.opportunities:
            if self._alerts is not None:
                await self._alerts.dispatch(opportunity)
            if self._should_paper_trade(opportunity):
                await self._paper_trade(opportunity)

    def _should_paper_trade(self, opportunity: Opportunity) -> bool:
        trial = self._settings.trial
        if not trial.auto_paper_trade:
            return False
        if opportunity.is_confirmed:
            return True
        # Research candidates are unapproved pairs. Simulating them yields execution
        # evidence without ever implying the pair is settlement-equivalent, but it is
        # off by default so trial results are not diluted by unverified pairs.
        return trial.paper_trade_research_candidates

    async def _paper_trade(self, opportunity: Opportunity) -> None:
        try:
            trade = await self._scanner.paper_trade(opportunity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error("paper_trade", exc)
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

    async def _maintenance_loop(self) -> None:
        while await self._sleep(MAINTENANCE_INTERVAL_SECONDS):
            try:
                unhealthy = self._check_health()
                if unhealthy:
                    self.stats.health_alerts += 1
                    log.error(
                        "trial.adapter_unhealthy",
                        venues=unhealthy,
                        note="ingestion is degraded; detected opportunity counts are "
                        "not meaningful while this persists",
                    )
                self.stats.snapshots_pruned += await self._scanner.prune_snapshots()
                # Re-fetch metadata so a mid-trial rules change is noticed promptly
                # rather than only at the next restart.
                await self._scanner.discover()
                await self._scanner.persist_markets()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error("maintenance", exc)

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
        }
