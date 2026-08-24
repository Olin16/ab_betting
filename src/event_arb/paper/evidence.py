"""How much a paper trade's leg-2 book is worth as evidence.

The interesting result of a paper trade is what happened to leg 2 while leg 1 was being
filled. That result is only *evidence* if the leg-2 book was actually re-observed after
the latency elapsed. Three things can happen, and they are not interchangeable:

``MEASURED``
    Leg 2 read a snapshot fetched from the venue after leg 1, and it differed from the one
    the opportunity was detected against. The adverse movement, or its absence, is real.

``UNCHANGED_REREAD``
    Leg 2 re-read and got a byte-identical book. Common on a quiet market and on fixture
    replay, where the file cannot change. A hedge success here says only "the book did not
    move in this sample", **never** "hedging succeeds under adverse movement" — there was
    no adverse movement to survive.

``MODELLED``
    No re-observation was possible, so leg 2 priced against a synthetic adverse move. The
    numbers describe the model's assumptions, not the venue.

The grade is recorded on every trade and carried into the daily report so a hedge-success
rate can never be read as measured when it was not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_arb.domain.models import OrderBook

__all__ = ["EvidenceGrade", "ProvidedBook", "grade_leg2"]


@dataclass(frozen=True)
class ProvidedBook:
    """A book handed to a leg, plus how it was obtained.

    ``refetched`` records whether the venue was actually asked again after the latency
    elapsed, and ``changed`` whether the answer differed from what was already held. Both
    are needed: a re-fetch that returns the same book proves nothing moved, which is a
    different claim from never having looked.
    """

    book: OrderBook | None
    refetched: bool = False
    changed: bool = False
    modelled: bool = False


class EvidenceGrade(StrEnum):
    MEASURED = "measured"
    UNCHANGED_REREAD = "unchanged_reread"
    MODELLED = "modelled"

    @property
    def supports_hedge_claims(self) -> bool:
        """Only a genuinely re-observed, changed book can support a hedge-rate claim."""
        return self is EvidenceGrade.MEASURED


def grade_leg2(*, refetched: bool, changed: bool, modelled: bool) -> EvidenceGrade:
    if modelled:
        return EvidenceGrade.MODELLED
    if refetched and changed:
        return EvidenceGrade.MEASURED
    return EvidenceGrade.UNCHANGED_REREAD
