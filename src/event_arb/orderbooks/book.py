"""In-memory store of the latest normalized book per (venue, market)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook
from event_arb.observability import metrics
from event_arb.orderbooks.freshness import FreshnessPolicy, FreshnessVerdict

__all__ = ["BookStore"]


class BookStore:
    """Holds the most recent book for each market and answers freshness questions."""

    def __init__(self, policy: FreshnessPolicy | None = None) -> None:
        self._books: dict[tuple[Venue, str], OrderBook] = {}
        self._policy = policy or FreshnessPolicy()
        self._updates: dict[tuple[Venue, str], int] = {}

    @property
    def policy(self) -> FreshnessPolicy:
        return self._policy

    def put(self, book: OrderBook) -> None:
        key = (book.venue, book.market_id)
        self._books[key] = book
        self._updates[key] = self._updates.get(key, 0) + 1

    def get(self, venue: Venue, market_id: str) -> OrderBook | None:
        return self._books.get((venue, market_id))

    def update_count(self, venue: Venue, market_id: str) -> int:
        return self._updates.get((venue, market_id), 0)

    def mark_stale(self, venue: Venue, market_id: str) -> None:
        """Force a book stale, e.g. after a disconnect or a sequence gap."""
        key = (venue, market_id)
        existing = self._books.get(key)
        if existing is not None and not existing.is_stale:
            self._books[key] = existing.model_copy(update={"is_stale": True})

    def evaluate(
        self, venue: Venue, market_id: str, now: datetime | None = None
    ) -> tuple[OrderBook | None, FreshnessVerdict | None]:
        book = self.get(venue, market_id)
        if book is None:
            return None, None
        verdict = self._policy.evaluate(book, now or datetime.now(UTC))
        metrics.book_age_ms.labels(venue=venue.value).observe(verdict.age_ms)
        return book, verdict

    def __iter__(self) -> Iterator[OrderBook]:
        return iter(self._books.values())

    def __len__(self) -> int:
        return len(self._books)
