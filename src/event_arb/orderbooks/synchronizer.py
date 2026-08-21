"""Pumps venue adapter streams into the :class:`BookStore`.

The synchronizer owns one task per adapter. Its responsibilities are narrow on purpose:
keep the store current, keep health current, and mark books stale the moment a feed is
in doubt. It never decides whether a book is *good enough* to trade on — that is the
freshness policy's job at evaluation time.

Polymarket has no sequence numbers on its market channel, so a periodic REST resync is
scheduled for that venue as the only available guard against silent divergence.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from event_arb.domain.enums import Venue
from event_arb.observability.logging import get_logger
from event_arb.orderbooks.book import BookStore
from event_arb.venues.base import VenueAdapter

__all__ = ["BookSynchronizer"]

log = get_logger(__name__)


class BookSynchronizer:
    """Runs adapter streams and keeps the book store fresh."""

    def __init__(
        self,
        store: BookStore,
        adapters: dict[Venue, VenueAdapter],
        resync_interval_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._adapters = adapters
        self._resync_interval = resync_interval_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._subscriptions: dict[Venue, list[str]] = {}

    async def prime(self, subscriptions: dict[Venue, list[str]]) -> None:
        """Fetch an initial REST snapshot for every subscribed market."""
        self._subscriptions = subscriptions
        for venue, market_ids in subscriptions.items():
            adapter = self._adapters.get(venue)
            if adapter is None:
                continue
            for market_id in market_ids:
                try:
                    book = await adapter.fetch_orderbook(market_id)
                except Exception as exc:  # a single bad market must not stop priming
                    log.warning(
                        "sync.prime_failed",
                        venue=venue.value,
                        market_id=market_id,
                        error=str(exc),
                    )
                    continue
                self._store.put(book)

    async def _pump(self, venue: Venue, adapter: VenueAdapter, market_ids: list[str]) -> None:
        try:
            async for event in adapter.stream(market_ids):
                self._store.put(event.book)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            adapter.health.connected = False
            adapter.health.record_error(str(exc))
            log.error("sync.stream_failed", venue=venue.value, error=str(exc))
            # Anything we hold for this venue is now unverifiable.
            for market_id in market_ids:
                self._store.mark_stale(venue, market_id)
            raise

    async def _periodic_resync(
        self, venue: Venue, adapter: VenueAdapter, market_ids: list[str]
    ) -> None:
        """REST-resync a venue that cannot prove stream continuity."""
        while True:
            await asyncio.sleep(self._resync_interval)
            for market_id in market_ids:
                try:
                    book = await adapter.fetch_orderbook(market_id)
                except Exception as exc:
                    log.warning(
                        "sync.resync_failed",
                        venue=venue.value,
                        market_id=market_id,
                        error=str(exc),
                    )
                    self._store.mark_stale(venue, market_id)
                    continue
                self._store.put(book)
                adapter.health.resync_count += 1

    async def start(self, subscriptions: dict[Venue, list[str]] | None = None) -> None:
        subs = subscriptions or self._subscriptions
        for venue, market_ids in subs.items():
            adapter = self._adapters.get(venue)
            if adapter is None or not market_ids:
                continue
            self._tasks.append(asyncio.create_task(self._pump(venue, adapter, market_ids)))
            if venue is Venue.POLYMARKET:
                self._tasks.append(
                    asyncio.create_task(self._periodic_resync(venue, adapter, market_ids))
                )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            # Shutdown is best-effort: a task that already failed has been logged.
            with suppress(BaseException):
                await task
        self._tasks.clear()

    def health_report(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        report: list[dict[str, object]] = []
        for venue, adapter in self._adapters.items():
            entry = adapter.health.as_dict()
            entry["fresh"] = adapter.health.is_fresh(self._store.policy.max_age, now)
            entry["venue"] = venue.value
            report.append(entry)
        return report
