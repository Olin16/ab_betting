"""What one refresh batch actually achieved, per required market.

A refresh that returns normally is not a refresh that worked. ``Scanner.refresh_books``
catches each ``fetch_orderbook`` failure, marks that book stale and carries on, so a total
venue outage and a completely healthy cycle both ended as "returned a list, raised
nothing". Every consumer downstream — the runtime's failure streak, the preflight
freshness gate, the CLI's exit code — read that silence as success, and an unusable feed
was reported as a quiet market.

The difference between those two cases is *coverage*: which of the markets this trial is
required to watch now have a usable book, and which do not. That is what this module
carries, so the callers can stop inferring health from the absence of an exception.

A market can fail to be covered in three distinct ways, kept distinct because they point
at different problems:

``failed``    the request raised — the venue is unreachable, rate-limiting, or erroring
``unusable``  a book came back with no executable depth, so nothing can be priced from it
``skipped``   the market is known but not tradable, so no request was made at all

All three mean the same thing for trading — no usable price for a market we must price —
which is why :attr:`RefreshOutcome.missing` unions them. They are reported separately
because "the venue is down" and "the market closed" need different operator responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from event_arb.domain.keys import MarketKey
from event_arb.domain.models import OrderBook

__all__ = ["RefreshOutcome", "has_executable_depth"]


def has_executable_depth(book: OrderBook) -> bool:
    """Whether anything could actually be bought from this book.

    Both legs of every direction this project trades are *buys* — YES on one venue, NO on
    the other — so the asks are what make a book priceable. A book with no asks on either
    outcome is syntactically valid and economically empty: the calculator walks its depth,
    finds nothing, and rejects. Counting that as a successful refresh is how a market that
    has stopped quoting gets reported as a market with no opportunities.
    """
    return bool(book.yes.asks) or bool(book.no.asks)


@dataclass
class RefreshOutcome:
    """Per-market result of one refresh batch.

    ``books`` holds only the books that are actually usable, so callers that persist or
    scan the batch never see an empty book they would have to filter themselves.
    """

    #: Every market this batch was asked to cover.
    required: tuple[MarketKey, ...] = ()
    #: Usable books fetched in this batch.
    books: list[OrderBook] = field(default_factory=list)
    #: Markets whose fetch raised, with the error text.
    failed: dict[MarketKey, str] = field(default_factory=dict)
    #: Markets that returned a book with no executable depth.
    unusable: dict[MarketKey, str] = field(default_factory=dict)
    #: Markets not requested because they are known to be untradable.
    skipped: dict[MarketKey, str] = field(default_factory=dict)
    #: Requests actually issued, which is what the rate-limit budget is spent on.
    requests: int = 0

    @property
    def covered(self) -> tuple[MarketKey, ...]:
        """Required markets that now have a usable book."""
        return tuple(book.key for book in self.books)

    @property
    def missing(self) -> tuple[MarketKey, ...]:
        """Required markets left without a usable book, however that happened."""
        covered = set(self.covered)
        return tuple(key for key in self.required if key not in covered)

    @property
    def is_complete(self) -> bool:
        """True only when every required market came back usable.

        Deliberately false for an empty batch with required markets, and deliberately
        *true* when nothing was required: a scanner with no watched pairs has not failed
        to refresh, it has nothing to refresh, and that is a different check's problem.
        """
        return not self.missing

    def reason(self) -> str:
        """One line naming what is missing and why. Empty when nothing is."""
        parts = []
        for label, entries in (
            ("unreachable", self.failed),
            ("no executable depth", self.unusable),
            ("not tradable", self.skipped),
        ):
            if entries:
                names = ", ".join(f"{k[0].value}:{k[2]}" for k in sorted(entries, key=str))
                parts.append(f"{label}: {names}")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "required": len(self.required),
            "covered": len(self.covered),
            "missing": len(self.missing),
            "failed": {f"{k[0].value}:{k[2]}": v for k, v in self.failed.items()},
            "unusable": {f"{k[0].value}:{k[2]}": v for k, v in self.unusable.items()},
            "skipped": {f"{k[0].value}:{k[2]}": v for k, v in self.skipped.items()},
            "requests": self.requests,
            "complete": self.is_complete,
        }
