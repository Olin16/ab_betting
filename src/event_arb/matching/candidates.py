"""Candidate generation for market pairs.

Candidates are *retrieval*, not truth. The scoring here decides which pairs a human
should look at, and nothing more. A perfect score of 1.0 still yields a pair with status
``suggested`` and ``manually_approved=False``, which the opportunity engine will only
ever label a *research candidate*.

Scoring blends four signals, weighted so that structural agreement dominates surface
wording:

- **token overlap** (Jaccard) and **fuzzy title ratio** — surface similarity;
- **date agreement** — two contracts on different dates are different contracts;
- **threshold agreement** — the numeric heart of the question.

Date or threshold *disagreement* applies a hard penalty rather than a small deduction,
because those are the cases where surface similarity is most misleading.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz

from event_arb.domain.enums import MatchStatus
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


def pair_id_for(kalshi_market_id: str, polymarket_market_id: str) -> str:
    """Deterministic pair identifier, stable across runs."""
    import hashlib

    digest = hashlib.sha256(f"{kalshi_market_id}|{polymarket_market_id}".encode()).hexdigest()
    return digest[:32]


@dataclass(frozen=True)
class CandidateScore:
    score: Decimal
    token_overlap: Decimal
    fuzzy_ratio: Decimal
    dates_agree: bool | None
    thresholds_agree: bool | None
    notes: tuple[str, ...] = ()


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
    dates_agree: bool | None
    if not kalshi_features.dates or not polymarket_features.dates:
        dates_agree = None
        date_component = Decimal("0.5")
        notes.append("no date extracted from at least one market")
    elif kalshi_features.dates & polymarket_features.dates:
        dates_agree = True
        date_component = Decimal("1")
    else:
        dates_agree = False
        date_component = Decimal("0")
        notes.append("extracted dates do not overlap")

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
            market.venue_market_id, market.title, market.rules_text
        )
        for market in kalshi_markets
    }
    p_features = {
        market.venue_market_id: NormalizedMarket.from_market(
            market.venue_market_id, market.title, market.rules_text
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
                id=pair_id_for(kalshi.venue_market_id, polymarket.venue_market_id),
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
