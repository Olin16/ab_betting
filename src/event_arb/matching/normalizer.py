"""Normalize market titles and rules into structured, comparable features.

Titles alone are never sufficient to establish settlement equivalence, but they are a
reasonable *retrieval* signal. This module extracts the structured features that make
retrieval meaningful — dates, numeric thresholds, comparison operators, entities — and
that later feed the rules diff.

Everything here is deliberately conservative: when a feature cannot be extracted with
confidence it is left as ``None`` rather than guessed, because a wrong threshold silently
pairs two different questions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from event_arb.domain.enums import ComparisonOperator

__all__ = ["NormalizedMarket", "extract_dates", "extract_threshold", "normalize_text", "tokenize"]

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "will",
        "be",
        "is",
        "are",
        "was",
        "were",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "and",
        "or",
        "if",
        "then",
        "that",
        "this",
        "it",
        "its",
        "as",
        "from",
        "than",
        "more",
        "less",
        "over",
        "under",
        "above",
        "below",
        "between",
        "market",
        "resolve",
        "resolves",
        "resolved",
        "yes",
        "no",
    }
)

_PUNCT_RE = re.compile(r"[^\w\s.%$-]+")
_WS_RE = re.compile(r"\s+")

#: Order matters: the two-character operators must be tried before the one-character ones.
_OPERATOR_PATTERNS: tuple[tuple[str, ComparisonOperator], ...] = (
    (
        r">=|at least|no less than|or (?:higher|more|greater|above)|greater than or equal",
        ComparisonOperator.GTE,
    ),
    (
        r"<=|at most|no more than|or (?:lower|less|fewer|below)|less than or equal",
        ComparisonOperator.LTE,
    ),
    (
        r"strictly (?:greater|more|above)|greater than|more than|above|over|exceeds?|>",
        ComparisonOperator.GT,
    ),
    (r"strictly (?:less|below)|less than|fewer than|below|under|<", ComparisonOperator.LT),
    (r"exactly|equal to|=", ComparisonOperator.EQ),
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

#: Idioms whose comparison words are not comparisons. Neutralized before operator search.
_IDIOM_RE = re.compile(r"year[\s-]*over[\s-]*year|yoy")

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TEXT_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)

#: A bare number preceded by a comparison word, optionally with $, %, or a unit suffix.
_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(%|percent|bps|basis points|degrees?|f\b|c\b)?")


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace."""
    lowered = text.lower().replace("\u2019", "'")
    stripped = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).strip()


def tokenize(text: str, drop_stopwords: bool = True) -> frozenset[str]:
    """Content tokens for overlap scoring."""
    tokens = normalize_text(text).split()
    if drop_stopwords:
        tokens = [token for token in tokens if token not in _STOPWORDS]
    return frozenset(tokens)


def extract_dates(text: str) -> frozenset[date]:
    """Pull ISO and "Aug 22 2026" style dates out of free text.

    A date without an explicit year is ignored rather than assumed to be the current
    year: pairing a 2026 contract with a 2027 one would be a settlement-equivalence error
    of exactly the kind this project exists to prevent.
    """
    found: set[date] = set()
    for match in _ISO_DATE_RE.finditer(text):
        try:
            found.add(date(int(match[1]), int(match[2]), int(match[3])))
        except ValueError:
            continue
    for match in _TEXT_DATE_RE.finditer(text):
        year = match[3]
        if year is None:
            continue
        month = _MONTHS.get(match[1][:3].lower())
        if month is None:
            continue
        try:
            found.add(date(int(year), month, int(match[2])))
        except ValueError:
            continue
    return frozenset(found)


def extract_threshold(text: str) -> tuple[Decimal | None, ComparisonOperator | None, str | None]:
    """Extract ``(threshold, operator, unit)`` from a question or rules sentence.

    Returns ``(None, None, None)`` when no confident extraction is possible. The
    inclusive/exclusive distinction (``>`` vs ``>=``) is preserved because it decides
    settlement at the boundary.

    Three sources of error are guarded against, each of which silently produced a wrong
    threshold before being handled:

    - **Idioms containing comparison words.** "year-over-year" is not a comparison; the
      ``over`` in it must not be read as ``>``.
    - **Trailing operators.** "90F or higher" puts the number *before* the operator,
      while "above 90F" puts it after. The number nearest the operator is chosen.
    - **Years masquerading as thresholds.** In "above 3.0% for August 2026", the bare
      ``2026`` is a date, not a threshold. Unit-less numbers that match a year already
      extracted as a date are skipped.
    """
    normalized = _IDIOM_RE.sub(" yoy ", text.lower())
    date_years = {d.year for d in extract_dates(text)}

    operator: ComparisonOperator | None = None
    operator_start = 0
    operator_end = 0
    for pattern, candidate in _OPERATOR_PATTERNS:
        match = re.search(pattern, normalized)
        if match is not None:
            operator = candidate
            operator_start = match.start()
            operator_end = match.end()
            break

    if operator is None:
        return None, None, None

    def plausible(match: re.Match[str]) -> bool:
        """Reject bare years; they are dates, not contract thresholds."""
        unit = match[2]
        if unit:
            return True
        try:
            value = Decimal(match[1])
        except InvalidOperation:
            return False
        return not (value == value.to_integral_value() and int(value) in date_years)

    # Candidates after the operator ("above 90F") and before it ("90F or higher"),
    # ranked by distance from the operator so the nearest plausible number wins.
    after = [
        (m.start(), m)
        for m in _NUMBER_RE.finditer(normalized[operator_end:])
        if plausible(m) and m[1]
    ]
    before = [
        (operator_start - m.end(), m)
        for m in _NUMBER_RE.finditer(normalized[:operator_start])
        if plausible(m) and m[1]
    ]
    candidates = sorted(after + before, key=lambda item: item[0])
    if not candidates:
        return None, operator, None

    number_match = candidates[0][1]
    try:
        value = Decimal(number_match[1])
    except InvalidOperation:
        return None, operator, None

    unit = number_match[2]
    if unit in {"percent", "%"}:
        unit = "%"
    elif unit in {"bps", "basis points"}:
        unit = "bps"
    elif unit in {"degrees", "degree", "f"}:
        unit = "F"
    return value, operator, unit


@dataclass(frozen=True)
class NormalizedMarket:
    """Structured features extracted from one venue market."""

    market_id: str
    normalized_title: str
    tokens: frozenset[str] = field(default_factory=frozenset)
    dates: frozenset[date] = field(default_factory=frozenset)
    threshold: Decimal | None = None
    operator: ComparisonOperator | None = None
    unit: str | None = None
    #: The venue's own scheduled close, carried through unmodified. This is *structured*
    #: data both venues always publish, unlike ``dates``, which is regex-extracted from
    #: prose and is empty whenever a title omits an explicit year — which live titles
    #: usually do. It corroborates the date signal; it never contradicts it.
    close_time: datetime | None = None

    @classmethod
    def from_market(
        cls,
        market_id: str,
        title: str,
        rules_text: str = "",
        close_time: datetime | None = None,
    ) -> NormalizedMarket:
        combined = f"{title} {rules_text}"
        # Thresholds are read from the title first: rules text often restates several
        # numbers (dates, deadlines) that are not the contract threshold.
        threshold, operator, unit = extract_threshold(title)
        if threshold is None:
            threshold, operator, unit = extract_threshold(rules_text)
        return cls(
            market_id=market_id,
            normalized_title=normalize_text(title),
            tokens=tokenize(title),
            dates=extract_dates(combined),
            threshold=threshold,
            operator=operator,
            unit=unit,
            close_time=close_time,
        )
