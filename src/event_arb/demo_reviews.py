"""Pre-recorded human reviews, for fixture demonstrations only.

There used to be a ``--auto-approve`` flag. It ran the automated rule comparison and, when
that found no material difference, wrote ``manually_approved = True`` — after which the
scanner labelled the resulting opportunities CONFIRMED. It also worked against live market
data. That inverted the project's core invariant: automated text comparison cannot
establish settlement equivalence, and only a human who has read both rulebooks may say that
two contracts settle the same way.

This replaces it. The reviews live in a checked-in file that a person wrote, naming exact
fixture market ids. Nothing here compares rules to *decide* anything — the decision was
already made by a human and recorded. Three properties keep it from becoming the old flag
again:

1. it refuses to run unless the data source is fixtures, so it can never touch live data;
2. it matches on exact fixture market ids, so it cannot generalise to a market a reviewer
   has not seen;
3. every approval it replays carries the reviewer's name and evidence through the same
   :class:`~event_arb.domain.models.ReviewedSource` machinery a real approval uses, and is
   still subject to every readiness check — a recorded review cannot force an approval the
   normal path would refuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from event_arb.config import DataSource, Settings
from event_arb.domain.models import ReviewedSource
from event_arb.observability.logging import get_logger

__all__ = ["DemoReviewError", "RecordedReview", "load_recorded_reviews"]

log = get_logger(__name__)

#: Lives beside the fixtures it refers to; the ids inside are meaningless anywhere else.
REVIEWS_FILENAME = "recorded_reviews.json"


class DemoReviewError(RuntimeError):
    """Raised when recorded reviews are requested somewhere they must not apply."""


@dataclass(frozen=True)
class RecordedReview:
    """One pair a named person reviewed, identified by exact venue market ids."""

    kalshi_market_id: str
    polymarket_market_id: str
    polymarket_product: str
    reviewed_by: str
    notes: str
    kalshi_source: ReviewedSource
    polymarket_source: ReviewedSource

    def matches(self, kalshi_market_id: str, polymarket_market_id: str, product: str) -> bool:
        return (
            self.kalshi_market_id == kalshi_market_id
            and self.polymarket_market_id == polymarket_market_id
            and self.polymarket_product == product
        )


def _source(payload: dict[str, str], recorded_by: str) -> ReviewedSource:
    return ReviewedSource(
        canonical_id=payload["canonical_id"],
        evidence=payload["evidence"],
        recorded_by=recorded_by,
    )


def load_recorded_reviews(settings: Settings) -> list[RecordedReview]:
    """Load the recorded reviews for the configured fixture corpus.

    Raises :class:`DemoReviewError` when the data source is anything but fixtures. That
    check is here, not at the call site, so no future caller can reach the replay path with
    live data by forgetting to guard it.
    """
    if settings.data_source is not DataSource.FIXTURE:
        raise DemoReviewError(
            "recorded reviews are fixture-only. They name specific fixture market ids and "
            "carry a human's review of those exact contracts; replaying them against live "
            "markets would attach a real reviewer's name to contracts they never read."
        )

    path = Path(settings.fixture_dir) / REVIEWS_FILENAME
    if not path.exists():
        return []

    entries = json.loads(path.read_text())["reviews"]
    reviews = [
        RecordedReview(
            kalshi_market_id=entry["kalshi_market_id"],
            polymarket_market_id=entry["polymarket_market_id"],
            polymarket_product=entry["polymarket_product"],
            reviewed_by=entry["reviewed_by"],
            notes=entry["notes"],
            kalshi_source=_source(entry["kalshi_source"], entry["reviewed_by"]),
            polymarket_source=_source(entry["polymarket_source"], entry["reviewed_by"]),
        )
        for entry in entries
    ]
    log.info("demo.recorded_reviews_loaded", count=len(reviews), path=str(path))
    return reviews
