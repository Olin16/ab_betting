"""Enumerations shared across the domain."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AlertChannel",
    "ArbDirection",
    "ComparisonOperator",
    "FeePolicy",
    "MatchStatus",
    "OpportunityLabel",
    "Outcome",
    "PaperTradeStatus",
    "RejectionReason",
    "RiskFlag",
    "SettlementAsset",
    "Venue",
    "VenueProduct",
]


class Venue(StrEnum):
    KALSHI = "KALSHI"
    POLYMARKET = "POLYMARKET"


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"

    @property
    def opposite(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class VenueProduct(StrEnum):
    """A distinct product within a venue brand.

    Polymarket Global (Adventure One QSS) and Polymarket US (QCX LLC) share a brand but are
    different venues: different operators, hosts, identifiers, settlement assets, fee models
    and terms. Collapsing both into ``Venue.POLYMARKET`` made the persisted uniqueness
    constraint stop meaning "one row per market on one venue", and left nothing to stop a
    US-configured scanner requesting a leftover Global market id.

    ``NONE`` is for venues that have exactly one product, such as Kalshi.
    """

    NONE = "none"
    POLYMARKET_GLOBAL = "polymarket_global"
    POLYMARKET_US = "polymarket_us"

    @property
    def is_polymarket(self) -> bool:
        return self in {VenueProduct.POLYMARKET_GLOBAL, VenueProduct.POLYMARKET_US}


class FeePolicy(StrEnum):
    """How a venue computes and rounds fees.

    Carried explicitly on :class:`~event_arb.domain.models.FeeConfig` rather than inferred
    from :class:`Venue`, because two Polymarket products share a venue value and round
    incompatibly: Global ceils to five decimals, US uses banker's rounding to the cent.
    Inferring from the venue would silently apply the wrong one.

    ``UNKNOWN`` never prices anything; the dispatcher refuses it.
    """

    KALSHI_CENTICENT_CEIL = "kalshi_centicent_ceil"
    POLYMARKET_GLOBAL_5DP_CEIL = "polymarket_global_5dp_ceil"
    POLYMARKET_US_BANKERS_CENT = "polymarket_us_bankers_cent"
    UNKNOWN = "unknown"


class SettlementAsset(StrEnum):
    """USD and USDC are deliberately distinct: they carry basis and depeg risk."""

    USD = "USD"
    USDC = "USDC"


class ComparisonOperator(StrEnum):
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"
    BETWEEN = "BETWEEN"

    @property
    def is_inclusive(self) -> bool:
        return self in {ComparisonOperator.GTE, ComparisonOperator.LTE, ComparisonOperator.EQ}


class MatchStatus(StrEnum):
    SUGGESTED = "suggested"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class OpportunityLabel(StrEnum):
    """Only manually approved pairs may ever be called an arbitrage."""

    CONFIRMED = "confirmed"
    RESEARCH_CANDIDATE = "research_candidate"


class ArbDirection(StrEnum):
    #: Buy Kalshi YES + Buy Polymarket NO
    A_KALSHI_YES_POLY_NO = "A_KALSHI_YES_POLY_NO"
    #: Buy Kalshi NO + Buy Polymarket YES
    B_KALSHI_NO_POLY_YES = "B_KALSHI_NO_POLY_YES"

    @property
    def kalshi_outcome(self) -> Outcome:
        return Outcome.YES if self is ArbDirection.A_KALSHI_YES_POLY_NO else Outcome.NO

    @property
    def polymarket_outcome(self) -> Outcome:
        return Outcome.NO if self is ArbDirection.A_KALSHI_YES_POLY_NO else Outcome.YES


class RiskFlag(StrEnum):
    """Non-fatal warnings attached to an opportunity. Fatal problems are rejections."""

    UNAPPROVED_PAIR = "unapproved_pair"
    STALE_BOOK_KALSHI = "stale_book_kalshi"
    STALE_BOOK_POLYMARKET = "stale_book_polymarket"
    THIN_BOOK = "thin_book"
    SINGLE_LEVEL_FILL = "single_level_fill"
    WIDE_SPREAD = "wide_spread"
    USD_USDC_BASIS = "usd_usdc_basis"
    NO_SEQUENCE_NUMBERS = "no_sequence_numbers"
    RULES_DIFFERENCES_PRESENT = "rules_differences_present"
    CLOSE_TIME_MISMATCH = "close_time_mismatch"
    NEAR_CLOSE = "near_close"
    RESYNC_RECENTLY_REQUIRED = "resync_recently_required"
    NON_ATOMIC_EXECUTION = "non_atomic_execution"


class RejectionReason(StrEnum):
    """Why a candidate opportunity was not emitted."""

    PAIR_NOT_APPROVED = "pair_not_approved"
    PAIR_SUSPENDED = "pair_suspended"
    BOOK_STALE = "book_stale"
    BOOK_INCOMPLETE = "book_incomplete"
    BOOK_MISSING = "book_missing"
    MARKET_NOT_TRADABLE = "market_not_tradable"
    NO_EXECUTABLE_QUANTITY = "no_executable_quantity"
    BELOW_MIN_PROFIT = "below_min_profit"
    BELOW_MIN_RETURN = "below_min_return"
    NEGATIVE_AFTER_FEES = "negative_after_fees"
    CROSSED_OR_INVALID_PRICES = "crossed_or_invalid_prices"
    COOLDOWN_ACTIVE = "cooldown_active"


class PaperTradeStatus(StrEnum):
    PENDING = "pending"
    LEG1_FILLED = "leg1_filled"
    BOTH_LEGS_FILLED = "both_legs_filled"
    PARTIALLY_HEDGED = "partially_hedged"
    LEG2_FAILED = "leg2_failed"
    SETTLED = "settled"


class AlertChannel(StrEnum):
    CONSOLE = "console"
    WEBHOOK = "webhook"
