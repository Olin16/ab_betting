"""Aggregated health reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from event_arb.domain.enums import Venue
from event_arb.observability import metrics
from event_arb.venues.base import VenueAdapter

__all__ = ["HealthReport", "build_health_report"]


@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    generated_at: datetime
    adapters: list[dict[str, Any]]
    books_tracked: int
    live_trading_possible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "generated_at": self.generated_at.isoformat(),
            "adapters": self.adapters,
            "books_tracked": self.books_tracked,
            # Surfaced on every health check so an operator can see at a glance that
            # this deployment cannot trade.
            "live_trading_possible": self.live_trading_possible,
        }


def build_health_report(
    adapters: dict[Venue, VenueAdapter],
    books_tracked: int,
    max_age: timedelta = timedelta(seconds=30),
    now: datetime | None = None,
) -> HealthReport:
    reference = now or datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    all_fresh = bool(adapters)

    for venue, adapter in adapters.items():
        fresh = adapter.health.is_fresh(max_age, reference)
        entry = adapter.health.as_dict()
        entry["fresh"] = fresh
        entries.append(entry)
        all_fresh = all_fresh and fresh
        metrics.venue_up.labels(venue=venue.value).set(1 if fresh else 0)

    return HealthReport(
        healthy=all_fresh,
        generated_at=reference,
        adapters=entries,
        books_tracked=books_tracked,
    )
