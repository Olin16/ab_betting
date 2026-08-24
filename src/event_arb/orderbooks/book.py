"""In-memory store of the latest normalized book per market.

Keyed by ``(venue, product, market_id)``. Venue and id alone are not an identity: the two
Polymarket products share ``Venue.POLYMARKET`` but have separate identifier spaces, so a
Global book could be handed back for a US market id and priced as if it were that market.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from event_arb.domain.keys import MarketKey
from event_arb.domain.models import OrderBook
from event_arb.observability import metrics
from event_arb.orderbooks.freshness import FreshnessPolicy, FreshnessVerdict

__all__ = ["BookStore"]


class BookStore:
    """Holds the most recent book for each market and answers freshness questions."""

    def __init__(self, policy: FreshnessPolicy | None = None) -> None:
        self._books: dict[MarketKey, OrderBook] = {}
        self._policy = policy or FreshnessPolicy()
        self._updates: dict[MarketKey, int] = {}

    @property
    def policy(self) -> FreshnessPolicy:
        return self._policy

    def put(self, book: OrderBook) -> None:
        key = book.key
        self._books[key] = book
        self._updates[key] = self._updates.get(key, 0) + 1

    def get(self, key: MarketKey) -> OrderBook | None:
        return self._books.get(key)

    def update_count(self, key: MarketKey) -> int:
        return self._updates.get(key, 0)

    def mark_stale(self, key: MarketKey) -> None:
        """Force a book stale, e.g. after a disconnect or a sequence gap."""
        existing = self._books.get(key)
        if existing is not None and not existing.is_stale:
            self._books[key] = existing.model_copy(update={"is_stale": True})

    def evaluate(
        self, key: MarketKey, now: datetime | None = None
    ) -> tuple[OrderBook | None, FreshnessVerdict | None]:
        book = self.get(key)
        if book is None:
            return None, None
        verdict = self._policy.evaluate(book, now or datetime.now(UTC))
        metrics.book_age_ms.labels(venue=key[0].value).observe(verdict.age_ms)
        return book, verdict

    def __iter__(self) -> Iterator[OrderBook]:
        return iter(self._books.values())

    def __len__(self) -> int:
        return len(self._books)
