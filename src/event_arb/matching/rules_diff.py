"""Structured comparison of two venues' contract rules.

This is the heart of the safety argument. Two markets can share a title, a date, and a
number and still settle differently — and when they do, an apparent "arbitrage" is
actually an unhedged directional bet on the discrepancy.

The dimensions compared here are the ones that decide money at the boundary:

======================= ====================================================
Dimension               Why it settles differently
======================= ====================================================
``threshold``           92.4 vs 92.4 is agreement; 90 vs 92 is not.
``comparison_operator`` ``>= 90`` pays at exactly 90; ``> 90`` does not.
``close_time``          A market closing earlier can settle on less information.
``timezone``            "August 22" is a different day in UTC than in ET.
``settlement_source``   NWS preliminary vs NOAA final can disagree.
``revision_policy``     One venue honours corrections, the other ignores them.
``postponement``        Postponed event: resolve No, or void and refund?
``early_closure``       Early close changes which observations count.
``settlement_deadline`` Different deadlines mean different capital lockup.
``invalid_outcome``     Ambiguous result: 50/50 split, void, or No?
======================= ====================================================

A difference marked ``is_material`` can change which side of the contract pays. Material
differences do not block a human from approving a pair — sometimes a difference is
genuinely irrelevant to a specific event — but they are always surfaced, recorded on the
pair, and attached to any resulting opportunity as a risk flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from event_arb.domain.models import RuleDifference, VenueMarket
from event_arb.matching.normalizer import NormalizedMarket

__all__ = ["RulesComparison", "compare_rules", "detect_revision_policy"]

#: Revision policy is classified into **equivalence classes**, not surface phrasings.
#: "Subsequent revisions are ignored" and "only the initial release is used" express the
#: same policy; reporting them as a difference would be a false positive, and false
#: positives train reviewers to click past the real warnings.
_REVISION_CLASSES: tuple[tuple[str, str], ...] = (
    # Checked first: an explicit commitment to revised data overrides first-print wording.
    (
        r"final(?:ized|ly)?\s+(?:corrected\s+)?(?:\w+\s+){0,3}(?:report|value|data|figure)"
        r"|revised\s+(?:value|figure|data)"
        r"|corrections?\s+(?:will\s+)?(?:be\s+)?(?:applied|used|reflected)",
        "final/revised value governs",
    ),
    (
        r"initial release only|only the initial release|first published|first print"
        r"|preliminary|subsequent revisions? are ignored"
        r"|revisions?[^.]{0,40}(?:do not|will not|are ignored|shall not)"
        r"|(?:later )?corrections?[^.]{0,40}do not change",
        "first print governs",
    ),
)

#: Postponement policy as **equivalence classes**, not surface wording. "resolves to No"
#: and "will settle to No" are the same policy; reporting them as different would be a
#: false positive, and false positives train reviewers to click past real warnings.
_POSTPONEMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"postpon\w*[^.]{0,90}(?:resolv|settl)\w*\s+to\s+no"
        r"|(?:resolv|settl)\w*\s+to\s+no[^.]{0,60}postpon",
        "postponement settles No",
    ),
    (
        r"postpon\w*[^.]{0,90}(?:void|cancel|refund)"
        r"|(?:void|cancel|refund)\w*[^.]{0,60}postpon",
        "postponement voids the market",
    ),
    (
        r"if the (?:event|game|meeting|match) is (?:cancel|postpon)|postpon",
        "postponement addressed, outcome unspecified",
    ),
)

_INVALID_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:50/50|split evenly|resolve.{0,20}0\.5)", "ambiguous resolves 50/50"),
    (
        r"(?:void|refund|cancel).{0,40}(?:ambiguous|unclear|cannot be determined)",
        "ambiguous voids the market",
    ),
    (r"umpire|discretion of.{0,30}(?:resolver|oracle|committee)", "resolver discretion"),
)


def _first_match(text: str, patterns: tuple[tuple[str, str], ...]) -> str | None:
    lowered = text.lower()
    for pattern, label in patterns:
        if re.search(pattern, lowered):
            return label
    return None


def detect_revision_policy(rules_text: str) -> str | None:
    """Classify how a rules text treats data revisions, or ``None`` if unstated.

    Returns an equivalence class ("first print governs" / "final/revised value governs")
    rather than a quotation, so that two venues expressing the same policy in different
    words compare equal.
    """
    return _first_match(rules_text, _REVISION_CLASSES)


@dataclass(frozen=True)
class RulesComparison:
    differences: tuple[RuleDifference, ...]

    @property
    def material(self) -> tuple[RuleDifference, ...]:
        return tuple(d for d in self.differences if d.is_material)

    @property
    def has_material(self) -> bool:
        return bool(self.material)


def _fmt(value: object) -> str:
    if value is None:
        return "(not stated)"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compare_rules(
    kalshi: VenueMarket,
    polymarket: VenueMarket,
    kalshi_features: NormalizedMarket | None = None,
    polymarket_features: NormalizedMarket | None = None,
    close_time_tolerance: timedelta = timedelta(minutes=5),
) -> RulesComparison:
    """Produce the structured, side-by-side rule differences for a candidate pair."""
    k_features = kalshi_features or NormalizedMarket.from_market(
        kalshi.venue_market_id, kalshi.title, kalshi.rules_text, kalshi.close_time
    )
    p_features = polymarket_features or NormalizedMarket.from_market(
        polymarket.venue_market_id, polymarket.title, polymarket.rules_text, polymarket.close_time
    )

    diffs: list[RuleDifference] = []

    def add(dimension: str, k: object, p: object, material: bool, explanation: str) -> None:
        diffs.append(
            RuleDifference(
                dimension=dimension,
                kalshi_value=_fmt(k),
                polymarket_value=_fmt(p),
                is_material=material,
                explanation=explanation,
            )
        )

    # --- threshold ---------------------------------------------------------------
    if k_features.threshold != p_features.threshold:
        add(
            "threshold",
            k_features.threshold,
            p_features.threshold,
            True,
            "The contracts are written on different numeric thresholds, so they can "
            "settle to opposite outcomes for the same real-world result.",
        )

    # --- inclusive vs exclusive comparison ---------------------------------------
    if k_features.operator != p_features.operator:
        k_op, p_op = k_features.operator, p_features.operator
        boundary_only = (
            k_op is not None and p_op is not None and k_op.is_inclusive != p_op.is_inclusive
        )
        add(
            "comparison_operator",
            k_op,
            p_op,
            True,
            "Inclusive vs exclusive comparison: the contracts disagree at exactly the "
            "threshold value."
            if boundary_only
            else "The comparison operators differ, so the contracts ask different questions.",
        )

    # --- unit --------------------------------------------------------------------
    if k_features.unit != p_features.unit and (k_features.unit or p_features.unit):
        add(
            "threshold_unit",
            k_features.unit,
            p_features.unit,
            True,
            "Thresholds are expressed in different units and are not directly comparable.",
        )

    # --- close time and timezone --------------------------------------------------
    k_close, p_close = kalshi.close_time, polymarket.close_time
    if k_close is None or p_close is None:
        add(
            "close_time",
            k_close,
            p_close,
            True,
            "At least one close time is unknown, so the trading windows cannot be "
            "shown to coincide.",
        )
    elif abs(k_close - p_close) > close_time_tolerance:
        add(
            "close_time",
            k_close,
            p_close,
            True,
            f"Close times differ by {abs(k_close - p_close)}. One venue keeps trading "
            "after the other has closed, which breaks the hedge.",
        )

    k_tz = k_close.tzinfo.tzname(k_close) if k_close and k_close.tzinfo else None
    p_tz = p_close.tzinfo.tzname(p_close) if p_close and p_close.tzinfo else None
    if k_tz != p_tz:
        add(
            "timezone",
            k_tz,
            p_tz,
            False,
            "Close timestamps are expressed in different timezones; compare the "
            "absolute instants, not the wall-clock text.",
        )

    # --- settlement source --------------------------------------------------------
    k_source = (kalshi.settlement_source or "").strip()
    p_source = (polymarket.settlement_source or "").strip()
    if k_source.lower() != p_source.lower():
        add(
            "settlement_source",
            k_source or None,
            p_source or None,
            True,
            "Different authoritative sources can publish different values for the same "
            "underlying event.",
        )

    # --- revision / recount policy -------------------------------------------------
    k_revision = detect_revision_policy(kalshi.rules_text)
    p_revision = detect_revision_policy(polymarket.rules_text)
    if k_revision != p_revision:
        add(
            "revision_policy",
            k_revision,
            p_revision,
            True,
            "The venues treat data revisions differently, so a correction can settle the "
            "two legs to the same side and destroy the hedge.",
        )

    # --- postponement / cancellation ------------------------------------------------
    k_postpone = _first_match(kalshi.rules_text, _POSTPONEMENT_PATTERNS)
    p_postpone = _first_match(polymarket.rules_text, _POSTPONEMENT_PATTERNS)
    if k_postpone != p_postpone:
        add(
            "postponement",
            k_postpone,
            p_postpone,
            True,
            "Postponement or cancellation is handled differently: one venue may resolve "
            "No while the other voids and refunds.",
        )

    # --- invalid / ambiguous outcomes ------------------------------------------------
    k_invalid = _first_match(kalshi.rules_text, _INVALID_PATTERNS)
    p_invalid = _first_match(polymarket.rules_text, _INVALID_PATTERNS)
    if k_invalid != p_invalid:
        add(
            "invalid_outcome",
            k_invalid,
            p_invalid,
            True,
            "Ambiguous or undeterminable outcomes are handled differently.",
        )

    # --- early closure ----------------------------------------------------------------
    k_early = "early clos" in kalshi.rules_text.lower()
    p_early = "early clos" in polymarket.rules_text.lower()
    if k_early != p_early:
        add(
            "early_closure",
            k_early,
            p_early,
            False,
            "Only one venue documents early closure behaviour.",
        )

    # --- settlement asset --------------------------------------------------------------
    if kalshi.settlement_asset != polymarket.settlement_asset:
        add(
            "settlement_asset",
            kalshi.settlement_asset,
            polymarket.settlement_asset,
            False,
            "Legs settle in different assets (USD vs USDC), introducing conversion and "
            "depeg risk. This is expected for Kalshi/Polymarket and is priced as a "
            "haircut rather than treated as a blocking mismatch.",
        )

    return RulesComparison(differences=tuple(diffs))
