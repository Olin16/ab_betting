"""Staleness policy for order books.

Freshness is judged from the **local receipt time**, never the exchange timestamp. If a
venue's clock runs fast, trusting its timestamp would make an old book look new — exactly
the direction of error that manufactures a phantom arbitrage. Exchange timestamps are
still recorded and are used to *detect* clock skew, which is reported but never used to
extend a book's life.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from event_arb.domain.models import OrderBook

__all__ = ["FreshnessPolicy", "FreshnessVerdict"]


@dataclass(frozen=True)
class FreshnessVerdict:
    is_fresh: bool
    age_ms: int
    reason: str
    clock_skew_ms: int | None = None


@dataclass(frozen=True)
class FreshnessPolicy:
    """Decides whether a book may be used for an executable calculation."""

    max_age_ms: int = 2000
    #: Absolute exchange-vs-local difference beyond which skew is reported.
    max_clock_skew_ms: int = 30_000

    def evaluate(self, book: OrderBook, now: datetime | None = None) -> FreshnessVerdict:
        reference = now or datetime.now(UTC)
        age_ms = book.age_ms(reference)

        skew_ms: int | None = None
        if book.exchange_timestamp is not None:
            delta = book.received_at - book.exchange_timestamp
            skew_ms = int(delta.total_seconds() * 1000)

        if book.is_stale:
            return FreshnessVerdict(False, age_ms, "book explicitly marked stale", skew_ms)
        if not book.is_complete:
            return FreshnessVerdict(False, age_ms, "book incomplete", skew_ms)
        if age_ms > self.max_age_ms:
            return FreshnessVerdict(
                False, age_ms, f"book age {age_ms}ms exceeds limit {self.max_age_ms}ms", skew_ms
            )
        if skew_ms is not None and abs(skew_ms) > self.max_clock_skew_ms:
            # Skew this large means we cannot reason about the feed at all.
            return FreshnessVerdict(
                False,
                age_ms,
                f"clock skew {skew_ms}ms exceeds {self.max_clock_skew_ms}ms",
                skew_ms,
            )
        return FreshnessVerdict(True, age_ms, "fresh", skew_ms)

    @property
    def max_age(self) -> timedelta:
        return timedelta(milliseconds=self.max_age_ms)
