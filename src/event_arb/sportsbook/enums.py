"""Enumerations for sportsbook odds data."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DataProvider",
    "MarketType",
    "OddsFormat",
    "Period",
    "PushBehavior",
    "QuoteStatus",
    "SportsbookRiskFlag",
]


class DataProvider(StrEnum):
    """Who supplied the quote.

    OddsJam and OpticOdds currently share one API — ``developer.oddsjam.com`` redirects to
    ``developer.opticodds.com``. Both identities are kept so provenance survives in stored
    data and a future divergence stays representable.
    """

    OPTICODDS = "opticodds"
    ODDSJAM = "oddsjam"
    FIXTURE = "fixture"


class OddsFormat(StrEnum):
    AMERICAN = "american"
    DECIMAL = "decimal"
    FRACTIONAL = "fractional"
    PROBABILITY = "probability"


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    PLAYER_PROP = "player_prop"
    TEAM_TOTAL = "team_total"
    OUTRIGHT = "outright"
    OTHER = "other"


class Period(StrEnum):
    """Which portion of the event settles the market."""

    FULL_GAME = "full_game"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    FIRST_QUARTER = "first_quarter"
    REGULATION = "regulation"
    OTHER = "other"


class PushBehavior(StrEnum):
    """What happens when the result lands exactly on the line.

    The difference between a returned stake and a graded loss is the whole economics of a
    whole-number line, so it is modelled explicitly rather than left implicit.
    """

    PUSH_REFUND = "push_refund"
    GRADED_LOSS = "graded_loss"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class QuoteStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class SportsbookRiskFlag(StrEnum):
    """Risks specific to acting on a sportsbook quote."""

    #: The provider published no stake limit, so executable size is unknown.
    LIQUIDITY_UNVERIFIED = "liquidity_unverified"
    #: Quote older than the configured freshness window.
    STALE_QUOTE = "stale_quote"
    #: Book has suspended the market.
    SUSPENDED_MARKET = "suspended_market"
    #: Execution is a manual human action, not an API order.
    MANUAL_EXECUTION_REQUIRED = "manual_execution_required"
    #: The book may refuse, limit, or void the bet after acceptance.
    BOOK_MAY_REFUSE_OR_LIMIT = "book_may_refuse_or_limit"
    #: Whole-number line: the event can land exactly on it.
    PUSH_POSSIBLE = "push_possible"
    #: Overtime inclusion differs or is unknown between the legs.
    OVERTIME_RULES_DIFFER = "overtime_rules_differ"
    #: Mapping to the exchange contract has not been human-approved.
    UNAPPROVED_MAPPING = "unapproved_mapping"
    #: Sportsbook and exchange may not settle from the same source.
    SETTLEMENT_SOURCE_DIFFERS = "settlement_source_differs"
    #: Availability depends on the operator's jurisdiction.
    JURISDICTION_DEPENDENT = "jurisdiction_dependent"
