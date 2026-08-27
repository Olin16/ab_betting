"""Candidate generation for market pairs.

Candidates are *retrieval*, not truth. The scoring here decides which pairs a human
should look at, and nothing more. A perfect score of 1.0 still yields a pair with status
``suggested`` and ``manually_approved=False``, which the opportunity engine will only
ever label a *research candidate*.

Scoring blends four signals, weighted so that structural agreement dominates surface
wording:

- **token overlap** (Jaccard) and **fuzzy title ratio** — surface similarity;
- **date agreement** — two contracts on different dates are different contracts. Dates
  come from the titles and rules when they carry an explicit year, and otherwise from the
  structured close time both venues publish, which is the usual case on live catalogues;
- **threshold agreement** — the numeric heart of the question.

Date or threshold *disagreement* applies a hard penalty rather than a small deduction,
because those are the cases where surface similarity is most misleading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from rapidfuzz import fuzz

from event_arb.domain.enums import MatchStatus, VenueProduct
from event_arb.domain.models import MarketPair, VenueMarket
from event_arb.matching.normalizer import NormalizedMarket
from event_arb.matching.rules_diff import RulesComparison, compare_rules

__all__ = ["CandidateScore", "generate_candidates", "pair_id_for", "score_pair"]

_WEIGHT_TOKENS = Decimal("0.30")
_WEIGHT_FUZZY = Decimal("0.20")
_WEIGHT_DATES = Decimal("0.25")
_WEIGHT_THRESHOLD = Decimal("0.25")

#: Applied multiplicatively when a structural signal actively contradicts.
_CONTRADICTION_PENALTY = Decimal("0.35")


def pair_id_for(
    kalshi_market_id: str,
    polymarket_market_id: str,
    polymarket_product: VenueProduct,
) -> str:
    """Deterministic pair identifier, stable across runs.

    ``polymarket_product`` is required rather than defaulted: a default would let a caller
    that forgot it silently mint a Global id for a US pair, which is the exact confusion
    this argument exists to prevent.

    Product is part of the identity. Without it, a Kalshi market paired against the same
    market id on each Polymarket product produced one id, so the two pairs shared approval
    state and snapshot history.

    Adding it changes every existing id, which **invalidates stored approvals** — pairs are
    re-proposed under new ids and must be re-reviewed. That is the intended cost: an
    approval recorded against an ambiguous identity was never safe to reuse.
    """
    import hashlib

    material = f"{kalshi_market_id}|{polymarket_product.value}|{polymarket_market_id}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class CandidateScore:
    score: Decimal
    token_overlap: Decimal
    fuzzy_ratio: Decimal
    dates_agree: bool | None
    thresholds_agree: bool | None
    notes: tuple[str, ...] = ()


def _closes_on_the_same_day(left: datetime | None, right: datetime | None) -> bool:
    """Whether two venue close times land on the same UTC calendar day.

    Compared in UTC because the two venues report in it; a naive timestamp is assumed UTC
    rather than rejected, so a mapper that drops the offset degrades to the old neutral
    behaviour instead of raising mid-scan.
    """
    if left is None or right is None:
        return False
    return _as_utc(left).date() == _as_utc(right).date()


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> Decimal:
    if not left or not right:
        return Decimal("0")
    intersection = len(left & right)
    union = len(left | right)
    return Decimal(intersection) / Decimal(union)


def score_pair(
    kalshi_features: NormalizedMarket, polymarket_features: NormalizedMarket
) -> CandidateScore:
    """Score one candidate pair in [0, 1]."""
    notes: list[str] = []

    tokens = _jaccard(kalshi_features.tokens, polymarket_features.tokens)
    fuzzy = Decimal(
        str(
            round(
                fuzz.token_set_ratio(
                    kalshi_features.normalized_title, polymarket_features.normalized_title
                )
                / 100.0,
                4,
            )
        )
    )

    # --- dates ---
    #
    # Three cases, kept apart deliberately. What a market *states* always outranks when it
    # happens to close, and the close time is only ever consulted where no statement exists
    # to outrank it.
    dates_agree: bool | None
    if kalshi_features.dates and polymarket_features.dates:
        # 1. Both state a date. Compare the statements and nothing else.
        if kalshi_features.dates & polymarket_features.dates:
            dates_agree = True
            date_component = Decimal("1")
        else:
            dates_agree = False
            date_component = Decimal("0")
            notes.append("extracted dates do not overlap")
    elif not kalshi_features.dates and not polymarket_features.dates:
        # 2. Neither states a date, which on live catalogues is the normal case:
        # ``extract_dates`` deliberately requires an explicit year, and venue titles
        # ("Fed decision in September?") rarely carry one. Falling straight through to the
        # neutral 0.5 discards a quarter of the score for almost every real pair and leaves
        # retrieval resting on surface wording alone.
        #
        # Both venues do publish a structured close, so use it: identical close *days* are
        # corroboration that two contracts settle the same event. Disagreement is not the
        # converse and must not be read as one — venues legitimately close the same
        # question at different times — so it falls back to neutral rather than
        # contradicting. Only a prose date conflict is ever disqualifying.
        if _closes_on_the_same_day(kalshi_features.close_time, polymarket_features.close_time):
            dates_agree = True
            date_component = Decimal("1")
            notes.append("no date in either title; venue close times fall on the same day")
        else:
            dates_agree = None
            date_component = Decimal("0.5")
            notes.append("no date extracted from either market")
    else:
        # 3. Exactly one states a date. Stay neutral, and in particular do not let the
        # close times award agreement: the stated date would have gone unread, so a market
        # explicitly about the 22nd could be handed full date agreement with one that
        # merely happens to close on the same day. There is nothing to compare the
        # statement against, and that is the honest answer rather than a reason to
        # substitute a weaker signal.
        dates_agree = None
        date_component = Decimal("0.5")
        notes.append("only one market states a date; close times are not read as agreement")

    # --- thresholds ---
    thresholds_agree: bool | None
    k_threshold = kalshi_features.threshold
    p_threshold = polymarket_features.threshold
    if k_threshold is None or p_threshold is None:
        thresholds_agree = None
        threshold_component = Decimal("0.5")
        notes.append("no numeric threshold extracted from at least one market")
    elif k_threshold == p_threshold:
        thresholds_agree = True
        threshold_component = Decimal("1")
        if kalshi_features.operator != polymarket_features.operator:
            notes.append("same threshold but different comparison operator")
    else:
        thresholds_agree = False
        threshold_component = Decimal("0")
        notes.append(f"thresholds differ: {k_threshold} vs {p_threshold}")

    score = (
        _WEIGHT_TOKENS * tokens
        + _WEIGHT_FUZZY * fuzzy
        + _WEIGHT_DATES * date_component
        + _WEIGHT_THRESHOLD * threshold_component
    )

    # A contradiction on a structural signal is disqualifying, not a minor deduction.
    if dates_agree is False or thresholds_agree is False:
        score *= _CONTRADICTION_PENALTY

    return CandidateScore(
        score=round(score, 4),
        token_overlap=round(tokens, 4),
        fuzzy_ratio=fuzzy,
        dates_agree=dates_agree,
        thresholds_agree=thresholds_agree,
        notes=tuple(notes),
    )


def generate_candidates(
    kalshi_markets: list[VenueMarket],
    polymarket_markets: list[VenueMarket],
    min_score: Decimal = Decimal("0.5"),
    max_per_market: int = 3,
) -> list[tuple[MarketPair, CandidateScore, RulesComparison]]:
    """Propose candidate pairs for human review, best first.

    Every returned pair has status ``suggested``. Nothing in this function can produce an
    approved pair; approval is exclusively a human action (:mod:`event_arb.matching.approval`).
    """
    k_features = {
        market.venue_market_id: NormalizedMarket.from_market(
            market.venue_market_id, market.title, market.rules_text, market.close_time
        )
        for market in kalshi_markets
    }
    p_features = {
        market.venue_market_id: NormalizedMarket.from_market(
            market.venue_market_id, market.title, market.rules_text, market.close_time
        )
        for market in polymarket_markets
    }

    results: list[tuple[MarketPair, CandidateScore, RulesComparison]] = []
    for kalshi in kalshi_markets:
        scored: list[tuple[CandidateScore, VenueMarket]] = []
        for polymarket in polymarket_markets:
            score = score_pair(
                k_features[kalshi.venue_market_id], p_features[polymarket.venue_market_id]
            )
            if score.score >= min_score:
                scored.append((score, polymarket))

        scored.sort(key=lambda item: item[0].score, reverse=True)
        for score, polymarket in scored[:max_per_market]:
            comparison = compare_rules(
                kalshi,
                polymarket,
                k_features[kalshi.venue_market_id],
                p_features[polymarket.venue_market_id],
            )
            pair = MarketPair(
                id=pair_id_for(
                    kalshi.venue_market_id,
                    polymarket.venue_market_id,
                    polymarket.product,
                ),
                kalshi_market_id=kalshi.venue_market_id,
                polymarket_market_id=polymarket.venue_market_id,
                # Carried from the market itself, never defaulted: a pair that forgot its
                # product could later be restored against the other one.
                polymarket_product=polymarket.product,
                status=MatchStatus.SUGGESTED,
                semantic_score=score.score,
                manually_approved=False,
                rule_differences=comparison.differences,
                reviewer_notes="; ".join(score.notes),
            )
            results.append((pair, score, comparison))

    results.sort(key=lambda item: item[1].score, reverse=True)
    return results
