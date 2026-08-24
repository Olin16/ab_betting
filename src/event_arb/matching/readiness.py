"""Approval readiness: can this pair be approved at all?

Deliberately separate from similarity scoring. A high score says two markets *look* alike;
readiness says whether we hold enough information to make the claim that they **settle
alike**. Those are different questions, and conflating them is how a plausible-looking pair
becomes an approved one.

The blocking conditions below are not warnings. Each describes a state in which the claim
"these settle identically" cannot honestly be made, so approval is refused rather than
surfaced as a risk to acknowledge. In particular, a generic acknowledgement must not be
able to convert a *known contradictory* pair into "settlement-equivalent" — that is not a
risk the reviewer is accepting, it is a statement that is false.

Where an exception is genuinely warranted, it goes through
:class:`~event_arb.domain.enums.MatchStatus.EXPERT_REVIEW`, which is separately named,
separately audited, and never yields a normal confirmed-arbitrage label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from event_arb.domain.enums import VenueProduct
from event_arb.domain.models import MarketPair, RuleDifference, VenueMarket

__all__ = [
    "MIN_AUTHORITATIVE_RULES_CHARS",
    "ReadinessBlocker",
    "ReadinessReport",
    "assess_readiness",
]

#: Below this, a rules text cannot plausibly carry enforceable settlement conditions.
MIN_AUTHORITATIVE_RULES_CHARS = 120

#: Phrases indicating actual settlement conditions rather than a summary or disclaimer.
_RULES_MARKERS = ("resolve", "settle", "if the", "expire", "official", "deadline")


class ReadinessBlocker(StrEnum):
    """A reason a pair cannot be approved as settlement-equivalent."""

    KALSHI_RULES_INSUFFICIENT = "kalshi_rules_insufficient"
    POLYMARKET_RULES_INSUFFICIENT = "polymarket_rules_insufficient"
    KALSHI_SETTLEMENT_SOURCE_UNKNOWN = "kalshi_settlement_source_unknown"
    POLYMARKET_SETTLEMENT_SOURCE_UNKNOWN = "polymarket_settlement_source_unknown"
    PRODUCT_IDENTITY_MISSING = "product_identity_missing"
    PRODUCT_IDENTITY_MISMATCHED = "product_identity_mismatched"
    OUTCOME_IDENTITY_UNRESOLVED = "outcome_identity_unresolved"
    KALSHI_CLOSE_TIME_MISSING = "kalshi_close_time_missing"
    POLYMARKET_CLOSE_TIME_MISSING = "polymarket_close_time_missing"
    SETTLEMENT_DEADLINE_UNKNOWN = "settlement_deadline_unknown"
    CONTRADICTORY_DIFFERENCE = "contradictory_difference"


#: Dimensions whose disagreement can settle the two legs to *opposite* outcomes. A pair
#: differing on any of these is not equivalent; no acknowledgement makes it so.
_CONTRADICTORY_DIMENSIONS = frozenset(
    {
        "threshold",
        "comparison_operator",
        "threshold_unit",
        "settlement_source",
        "revision_policy",
        "postponement",
        "invalid_outcome",
    }
)


@dataclass(frozen=True)
class ReadinessReport:
    """What is known, what is missing, and whether approval may proceed."""

    blockers: tuple[tuple[ReadinessBlocker, str], ...] = ()
    missing_information: tuple[str, ...] = ()
    contradictory_dimensions: tuple[str, ...] = field(default=())

    @property
    def is_ready(self) -> bool:
        """True when a human may approve this pair through the normal path."""
        return not self.blockers

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(code.value for code, _ in self.blockers)

    def as_dict(self) -> dict[str, object]:
        return {
            "ready_for_approval": self.is_ready,
            "blockers": [{"code": code.value, "detail": detail} for code, detail in self.blockers],
            "missing_information": list(self.missing_information),
            "contradictory_dimensions": list(self.contradictory_dimensions),
        }


def _rules_are_authoritative(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < MIN_AUTHORITATIVE_RULES_CHARS:
        return False
    lowered = stripped.lower()
    return sum(marker in lowered for marker in _RULES_MARKERS) >= 2


def assess_readiness(
    pair: MarketPair,
    kalshi: VenueMarket,
    polymarket: VenueMarket,
    differences: tuple[RuleDifference, ...] | None = None,
    kalshi_source_override: str = "",
    polymarket_source_override: str = "",
    settlement_deadline_required: bool = False,
) -> ReadinessReport:
    """Decide whether a pair may be approved, and record why not.

    ``*_source_override`` lets a reviewer record an authoritative resolution source they
    obtained separately, for venues that publish none. It must be supplied **per leg**:
    two unknown sources are not agreement, they are two unknowns, and treating them as a
    match is the specific failure this function exists to prevent.
    """
    blockers: list[tuple[ReadinessBlocker, str]] = []
    missing: list[str] = []

    # --- authoritative rules --------------------------------------------------
    if not _rules_are_authoritative(kalshi.rules_text):
        blockers.append(
            (
                ReadinessBlocker.KALSHI_RULES_INSUFFICIENT,
                f"Kalshi rules text is {len(kalshi.rules_text.strip())} chars and lacks "
                "settlement language; the full rulebook must be obtained separately",
            )
        )
    if not _rules_are_authoritative(polymarket.rules_text):
        blockers.append(
            (
                ReadinessBlocker.POLYMARKET_RULES_INSUFFICIENT,
                f"Polymarket rules text is {len(polymarket.rules_text.strip())} chars and "
                "lacks settlement language; the full rulebook must be obtained separately",
            )
        )

    # --- authoritative settlement source --------------------------------------
    kalshi_source = (kalshi.settlement_source or kalshi_source_override).strip()
    polymarket_source = (polymarket.settlement_source or polymarket_source_override).strip()
    if not kalshi_source:
        blockers.append(
            (
                ReadinessBlocker.KALSHI_SETTLEMENT_SOURCE_UNKNOWN,
                "Kalshi publishes no settlement source; a reviewer must record one "
                "obtained separately",
            )
        )
        missing.append("kalshi settlement source")
    if not polymarket_source:
        blockers.append(
            (
                ReadinessBlocker.POLYMARKET_SETTLEMENT_SOURCE_UNKNOWN,
                "Polymarket publishes no settlement source; a reviewer must record one "
                "obtained separately",
            )
        )
        missing.append("polymarket settlement source")

    # --- product identity ------------------------------------------------------
    if polymarket.product is VenueProduct.NONE:
        blockers.append(
            (
                ReadinessBlocker.PRODUCT_IDENTITY_MISSING,
                "Polymarket market carries no product identity",
            )
        )
    elif pair.polymarket_product is not polymarket.product:
        blockers.append(
            (
                ReadinessBlocker.PRODUCT_IDENTITY_MISMATCHED,
                f"pair expects {pair.polymarket_product.value!r} but the market is "
                f"{polymarket.product.value!r}",
            )
        )

    # --- outcome identity ------------------------------------------------------
    if len(kalshi.outcomes) < 2 or len(polymarket.outcomes) < 2:
        blockers.append(
            (
                ReadinessBlocker.OUTCOME_IDENTITY_UNRESOLVED,
                "one or both legs do not expose a two-sided outcome set",
            )
        )

    # --- close time ------------------------------------------------------------
    if kalshi.close_time is None:
        blockers.append((ReadinessBlocker.KALSHI_CLOSE_TIME_MISSING, "Kalshi close time unknown"))
        missing.append("kalshi close time")
    if polymarket.close_time is None:
        blockers.append(
            (
                ReadinessBlocker.POLYMARKET_CLOSE_TIME_MISSING,
                "Polymarket close time unknown",
            )
        )
        missing.append("polymarket close time")

    # --- settlement deadline ---------------------------------------------------
    if settlement_deadline_required:
        for name, market in (("kalshi", kalshi), ("polymarket", polymarket)):
            if market.settlement_deadline is None:
                blockers.append(
                    (
                        ReadinessBlocker.SETTLEMENT_DEADLINE_UNKNOWN,
                        f"{name} settlement deadline is required for this pair but unknown",
                    )
                )
                missing.append(f"{name} settlement deadline")

    # --- contradictory differences ---------------------------------------------
    diffs = differences if differences is not None else pair.rule_differences
    contradictory = tuple(
        sorted(
            {
                d.dimension
                for d in diffs
                if d.is_material and d.dimension in _CONTRADICTORY_DIMENSIONS
            }
        )
    )
    for dimension in contradictory:
        blockers.append(
            (
                ReadinessBlocker.CONTRADICTORY_DIFFERENCE,
                f"{dimension} can settle the two legs to opposite outcomes; this is not "
                "a risk to acknowledge, it is a statement that they are not equivalent",
            )
        )

    return ReadinessReport(
        blockers=tuple(blockers),
        missing_information=tuple(missing),
        contradictory_dimensions=contradictory,
    )
