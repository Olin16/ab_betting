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

__all__ = ["FreshnessPolicy", "FreshnessVerdict", "RefreshTiming"]


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


@dataclass
class RefreshTiming:
    """Observed timing of REST refresh batches.

    The freshness bound has to be justified by what refreshing actually costs, not by a
    guessed constant. A bound below the observed cost rejects every book; a bound far above
    it silently accepts genuinely stale prices. Measuring closes both gaps, and measuring
    the **batch** matters because books are refreshed serially: the first market fetched is
    already one batch-duration old by the time the scan runs.
    """

    #: Wall-clock seconds for the most recent full refresh batch.
    last_batch_seconds: float = 0.0
    #: Requests issued in that batch.
    last_batch_requests: int = 0
    #: Slowest batch observed, which is what a bound must actually accommodate.
    worst_batch_seconds: float = 0.0
    batches_observed: int = 0
    #: Slowest observed gap between a batch starting and the scan that consumed it. Covers
    #: the batch itself plus everything the runtime does in between — currently persisting
    #: the snapshots. Measured rather than assumed, because that work grows with the number
    #: of watched markets just as the batch does.
    worst_batch_to_scan_seconds: float = 0.0
    scans_observed: int = 0

    def record(self, duration_seconds: float, requests: int) -> None:
        self.last_batch_seconds = duration_seconds
        self.last_batch_requests = requests
        self.worst_batch_seconds = max(self.worst_batch_seconds, duration_seconds)
        self.batches_observed += 1

    def record_scan_lag(self, seconds: float) -> None:
        """Time from the start of a refresh batch to the scan that used it."""
        self.worst_batch_to_scan_seconds = max(self.worst_batch_to_scan_seconds, seconds)
        self.scans_observed += 1

    @property
    def worst_book_age_seconds(self) -> float:
        """Age of the oldest book a scan can see, by observation.

        The runtime refreshes and then scans **within one cycle**, and skips the scan
        entirely when the refresh fails, so no book survives into a later cycle's scan. The
        oldest book a scan sees is therefore the first one fetched in the batch that
        immediately preceded it — roughly one batch old, not one cycle plus one batch.

        Prefer the measured batch-to-scan lag when a scan has been observed; it covers the
        batch plus the persist step between them. Fall back to the batch alone otherwise.
        """
        return max(self.worst_batch_to_scan_seconds, self.worst_batch_seconds)

    def required_age_ms(self, clock_tolerance_seconds: float) -> int:
        """Smallest bound a cycle of this observed cost can actually satisfy.

        The cycle interval is deliberately **not** a term. It used to be, on the assumption
        that a scan could land on the previous cycle's books; sequencing refresh before scan
        removed that possibility, but the formula kept the term. The effect was to demand a
        bound several times larger than reality and to push operators toward raising
        ``max_book_age_ms`` — loosening the very check that catches genuinely stale prices.
        """
        return int((self.worst_book_age_seconds + clock_tolerance_seconds) * 1000)

    def as_dict(self) -> dict[str, object]:
        return {
            "last_batch_seconds": round(self.last_batch_seconds, 3),
            "last_batch_requests": self.last_batch_requests,
            "worst_batch_seconds": round(self.worst_batch_seconds, 3),
            "batches_observed": self.batches_observed,
            "worst_batch_to_scan_seconds": round(self.worst_batch_to_scan_seconds, 3),
            "scans_observed": self.scans_observed,
        }
