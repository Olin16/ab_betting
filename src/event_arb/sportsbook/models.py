"""Provider-neutral sportsbook odds models.

These deliberately do **not** reuse :class:`~event_arb.domain.models.OrderBook`. A
sportsbook quote is a price a book is currently willing to offer, not a resting order with
published depth, and the differences decide money:

- there is one price, not a ladder of levels;
- executable size is whatever the book allows, which is often unpublished;
- the book may refuse, limit, or void the bet after you act;
- execution is a manual human action, not an API order.

Reusing the exchange abstraction would import all four of those assumptions silently. So
sportsbook data gets its own shape, and :attr:`SportsbookQuote.max_stake` is ``None`` when
the provider published no limit — never a default, never an assumption.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from event_arb.domain.models import rules_hash
from event_arb.sportsbook.enums import (
    DataProvider,
    MarketType,
    OddsFormat,
    Period,
    PushBehavior,
    QuoteStatus,
    SportsbookRiskFlag,
)
from event_arb.sportsbook.odds import parse_odds

__all__ = [
    "Competitor",
    "SportsbookEvent",
    "SportsbookMarket",
    "SportsbookQuote",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Competitor(_Frozen):
    """One side of a fixture.

    ``is_home`` is explicit because home/away inversion silently turns a spread into a bet
    on the other team, and provider naming alone cannot be relied upon to disambiguate.
    """

    id: str
    name: str
    abbreviation: str = ""
    is_home: bool | None = None


class SportsbookEvent(_Frozen):
    """A fixture as one provider sees it."""

    provider: DataProvider
    provider_event_id: str
    #: The book's own event identifier, when the provider exposes it.
    sportsbook_event_id: str | None = None
    sport: str
    league: str
    competitors: tuple[Competitor, ...] = ()
    scheduled_start: datetime | None = None
    #: IANA zone the venue schedules in. "August 22" is a different day in ET than in UTC.
    timezone: str = "UTC"
    status: str = ""
    is_live: bool = False
    raw_metadata: dict[str, object] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=_utcnow)

    @property
    def home(self) -> Competitor | None:
        return next((c for c in self.competitors if c.is_home is True), None)

    @property
    def away(self) -> Competitor | None:
        return next((c for c in self.competitors if c.is_home is False), None)

    @property
    def settlement_fingerprint(self) -> str:
        """Hash of the fields a reviewer weighs when approving a mapping."""
        return rules_hash(
            "sb-event-v1",
            self.sport,
            self.league,
            "|".join(sorted(c.name for c in self.competitors)),
            self.scheduled_start.isoformat() if self.scheduled_start else None,
            self.timezone,
        )


class SportsbookMarket(_Frozen):
    """A market on an event, independent of which book is quoting it."""

    provider: DataProvider
    provider_market_id: str
    event_id: str
    market_type: MarketType
    #: Provider's own label, kept verbatim because normalization can lose meaning.
    market_label: str = ""
    period: Period = Period.FULL_GAME
    #: True when the market settles including overtime. ``None`` means the provider did
    #: not say, which is itself material and must not be read as either value.
    includes_overtime: bool | None = None
    push_behavior: PushBehavior = PushBehavior.UNKNOWN
    settlement_source: str = ""
    raw_metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def settlement_fingerprint(self) -> str:
        return rules_hash(
            "sb-market-v1",
            self.market_type.value,
            self.market_label,
            self.period.value,
            "unknown" if self.includes_overtime is None else str(self.includes_overtime),
            self.push_behavior.value,
            self.settlement_source,
        )


class SportsbookQuote(_Frozen):
    """One price, from one book, on one selection, at one moment."""

    provider: DataProvider
    #: The book actually offering the price (DraftKings, FanDuel, ...). Distinct from the
    #: data provider that relayed it.
    sportsbook: str
    provider_event_id: str
    sportsbook_event_id: str | None = None
    provider_market_id: str
    market_type: MarketType
    period: Period = Period.FULL_GAME
    sport: str = ""
    league: str = ""
    #: The side being priced, verbatim from the provider.
    selection: str
    normalized_selection: str = ""
    #: Line or threshold: spread, total, or prop line. ``None`` for moneylines.
    line: Decimal | None = None
    #: Odds exactly as published, in :attr:`odds_format`.
    odds: Decimal
    odds_format: OddsFormat = OddsFormat.AMERICAN
    #: Largest stake the book published. ``None`` means no published limit — which is not
    #: the same as unlimited, and must never be defaulted to a number.
    max_stake: Decimal | None = None
    currency: str = "USD"
    status: QuoteStatus = QuoteStatus.ACTIVE
    #: When the provider says the price was observed.
    observed_at: datetime | None = None
    #: When this process received it. Freshness is judged from here, never from the
    #: provider clock, for the same reason exchange books are.
    received_at: datetime = Field(default_factory=_utcnow)
    is_main_line: bool = True
    raw_metadata: dict[str, object] = Field(default_factory=dict)
    is_stale: bool = False

    @field_validator("odds")
    @classmethod
    def _odds_present(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("odds of 0 are not meaningful")
        return value

    @property
    def decimal_odds(self) -> Decimal:
        """Odds as decimal odds, exactly."""
        return parse_odds(self.odds, self.odds_format)

    @property
    def has_published_limit(self) -> bool:
        return self.max_stake is not None and self.max_stake > 0

    @property
    def is_actionable(self) -> bool:
        """Active, not stale. Says nothing about whether the mapping is approved."""
        return self.status is QuoteStatus.ACTIVE and not self.is_stale

    def age_ms(self, now: datetime | None = None) -> int:
        reference = now or _utcnow()
        return max(0, int((reference - self.received_at).total_seconds() * 1000))

    @property
    def quote_key(self) -> str:
        """Stable identity for deduplication across repeated provider updates."""
        return ":".join(
            [
                self.provider.value,
                self.sportsbook,
                self.provider_event_id,
                self.provider_market_id,
                self.normalized_selection or self.selection,
                "" if self.line is None else format(self.line, "f"),
            ]
        )

    def risk_flags(self) -> tuple[SportsbookRiskFlag, ...]:
        """Risks inherent to this quote, before any mapping is considered."""
        flags: set[SportsbookRiskFlag] = {
            # Always true: nothing in this system can place a bet.
            SportsbookRiskFlag.MANUAL_EXECUTION_REQUIRED,
            # Always true: acceptance is at the book's discretion.
            SportsbookRiskFlag.BOOK_MAY_REFUSE_OR_LIMIT,
        }
        if not self.has_published_limit:
            flags.add(SportsbookRiskFlag.LIQUIDITY_UNVERIFIED)
        if self.is_stale:
            flags.add(SportsbookRiskFlag.STALE_QUOTE)
        if self.status is QuoteStatus.SUSPENDED:
            flags.add(SportsbookRiskFlag.SUSPENDED_MARKET)
        if self.line is not None and self.line == self.line.to_integral_value():
            # A whole-number line can land exactly on the result.
            flags.add(SportsbookRiskFlag.PUSH_POSSIBLE)
        return tuple(sorted(flags, key=lambda flag: flag.value))
