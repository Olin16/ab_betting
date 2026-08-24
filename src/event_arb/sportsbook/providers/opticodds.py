"""OpticOdds payload mapper.

Also serves **OddsJam**: ``developer.oddsjam.com`` returns 301 to
``developer.opticodds.com``, so they are one API with one schema family. The
:class:`~event_arb.sportsbook.enums.DataProvider` value is carried through so provenance
survives in stored data, but the parsing is shared rather than duplicated into two
copies that would drift.

Schema verified from documentation on 2026-08-23; **no API calls have been made**. See
``docs/api-assumptions.md`` section D.

Two ingestion rules matter more than the rest:

- ``price`` and ``points`` arrive as JSON *numbers*. Payloads must be decoded with
  ``json.loads(..., parse_float=Decimal)``; this module refuses floats rather than
  silently accepting a value whose precision is already lost.
- ``limits.max`` is nullable. Absent means "no published limit", which is not "unlimited".
  It is carried through as ``None`` and drives a liquidity risk flag downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from event_arb.sportsbook.base import ProviderDataError
from event_arb.sportsbook.enums import (
    DataProvider,
    MarketType,
    OddsFormat,
    Period,
    QuoteStatus,
)
from event_arb.sportsbook.models import Competitor, SportsbookEvent, SportsbookQuote

__all__ = ["parse_events", "parse_quotes", "parse_stream_frame"]

#: Documented base. Recorded here for the eventual live client; nothing calls it today.
BASE_URL = "https://api.opticodds.com/api/v3"

#: Provider market_id -> normalized type. Unknown ids map to OTHER rather than being
#: guessed at, because a mis-typed market is a mis-priced comparison.
_MARKET_TYPES: dict[str, MarketType] = {
    "moneyline": MarketType.MONEYLINE,
    "point_spread": MarketType.SPREAD,
    "spread": MarketType.SPREAD,
    "total_points": MarketType.TOTAL,
    "total": MarketType.TOTAL,
    "team_total": MarketType.TEAM_TOTAL,
}

_PERIODS: tuple[tuple[str, Period], ...] = (
    ("1st_half", Period.FIRST_HALF),
    ("first_half", Period.FIRST_HALF),
    ("2nd_half", Period.SECOND_HALF),
    ("1st_quarter", Period.FIRST_QUARTER),
    ("regulation", Period.REGULATION),
)


def _decimal(value: Any, field: str) -> Decimal:
    """Coerce a JSON value to Decimal, refusing anything already through a float."""
    if isinstance(value, float):
        raise ProviderDataError(
            f"{field} arrived as a float; decode provider JSON with parse_float=Decimal"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise ProviderDataError(f"unparseable {field}: {value!r}") from exc
    raise ProviderDataError(f"unsupported {field} type {type(value).__name__}")


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    return None if value is None else _decimal(value, field)


def _timestamp(value: Any) -> datetime | None:
    """ASSUMPTION: epoch seconds. The documentation does not state the unit."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(_decimal(value, "timestamp")), tz=UTC)
    except (ProviderDataError, ValueError, OSError, OverflowError):
        return None


def _market_type(market_id: str) -> MarketType:
    return _MARKET_TYPES.get(market_id.strip().lower(), MarketType.OTHER)


def _period(market_id: str) -> Period:
    lowered = market_id.strip().lower()
    for token, period in _PERIODS:
        if token in lowered:
            return period
    return Period.FULL_GAME


def _competitors(entry: dict[str, Any]) -> tuple[Competitor, ...]:
    """Build competitors with explicit home/away orientation.

    Orientation is recorded rather than inferred later: an inverted spread is a bet on the
    other team, and nothing downstream can recover the distinction from names alone.
    """
    people: list[Competitor] = []
    for key, is_home in (("home_competitors", True), ("away_competitors", False)):
        for raw in entry.get(key, []) or []:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise ProviderDataError(f"competitor in {key} has no name")
            people.append(
                Competitor(
                    id=str(raw.get("id", "")),
                    name=name,
                    abbreviation=str(raw.get("abbreviation", "")),
                    is_home=is_home,
                )
            )
    return tuple(people)


def _entries(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise ProviderDataError("provider payload 'data' must be a list")
    return [entry for entry in data if isinstance(entry, dict)]


def parse_events(payload: Any, provider: DataProvider) -> list[SportsbookEvent]:
    """Normalize a fixtures payload."""
    events: list[SportsbookEvent] = []
    for entry in _entries(payload):
        event_id = entry.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ProviderDataError("fixture entry missing 'id'")

        start_raw = entry.get("start_date")
        start: datetime | None = None
        if isinstance(start_raw, str):
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                start = None

        sport = entry.get("sport")
        league = entry.get("league")
        events.append(
            SportsbookEvent(
                provider=provider,
                provider_event_id=event_id,
                sportsbook_event_id=(
                    entry.get("game_id") if isinstance(entry.get("game_id"), str) else None
                ),
                sport=(sport.get("name", "") if isinstance(sport, dict) else str(sport or "")),
                league=(league.get("name", "") if isinstance(league, dict) else str(league or "")),
                competitors=_competitors(entry),
                scheduled_start=start,
                status=str(entry.get("status", "")),
                is_live=bool(entry.get("is_live", False)),
                raw_metadata=dict(entry),
            )
        )
    return events


def _quote_from_odds(
    odds: dict[str, Any],
    provider: DataProvider,
    event_id: str,
    game_id: str | None,
    sport: str,
    league: str,
    status: QuoteStatus,
    received_at: datetime | None = None,
) -> SportsbookQuote:
    sportsbook = odds.get("sportsbook")
    if not isinstance(sportsbook, str) or not sportsbook:
        raise ProviderDataError("odds entry missing 'sportsbook'")
    if "price" not in odds or odds["price"] is None:
        raise ProviderDataError("odds entry missing 'price'")

    selection = odds.get("selection") or odds.get("name")
    if not isinstance(selection, str) or not selection:
        raise ProviderDataError("odds entry missing 'selection'")

    market_id = str(odds.get("market_id", "") or odds.get("market", ""))

    # limits.max is nullable, and its absence is meaningful. Do not default it.
    limits = odds.get("limits")
    max_stake = (
        _optional_decimal(limits.get("max"), "limits.max") if isinstance(limits, dict) else None
    )

    return SportsbookQuote(
        provider=provider,
        sportsbook=sportsbook,
        provider_event_id=event_id,
        sportsbook_event_id=game_id,
        provider_market_id=market_id,
        market_type=_market_type(market_id),
        period=_period(market_id),
        sport=sport,
        league=league,
        selection=selection,
        normalized_selection=str(odds.get("normalized_selection", "")),
        line=_optional_decimal(odds.get("points"), "points"),
        odds=_decimal(odds["price"], "price"),
        # The provider default; a live client must pass odds_format explicitly and set
        # this to match, or every downstream payout is wrong by a format.
        odds_format=OddsFormat.AMERICAN,
        max_stake=max_stake,
        status=status,
        observed_at=_timestamp(odds.get("timestamp")),
        received_at=received_at or datetime.now(UTC),
        is_main_line=bool(odds.get("is_main", True)),
        raw_metadata=dict(odds),
    )


def parse_quotes(payload: Any, provider: DataProvider) -> list[SportsbookQuote]:
    """Normalize a ``/fixtures/odds`` payload into flat quotes."""
    quotes: list[SportsbookQuote] = []
    for entry in _entries(payload):
        event_id = entry.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ProviderDataError("odds entry missing fixture 'id'")

        sport = entry.get("sport")
        league = entry.get("league")
        sport_name = sport.get("name", "") if isinstance(sport, dict) else str(sport or "")
        league_name = league.get("name", "") if isinstance(league, dict) else str(league or "")
        game_id = entry.get("game_id") if isinstance(entry.get("game_id"), str) else None

        raw_odds = entry.get("odds", [])
        if not isinstance(raw_odds, list):
            raise ProviderDataError("fixture 'odds' must be a list")
        for odds in raw_odds:
            if not isinstance(odds, dict):
                raise ProviderDataError("odds entry must be an object")
            quotes.append(
                _quote_from_odds(
                    odds,
                    provider,
                    event_id,
                    game_id,
                    sport_name,
                    league_name,
                    QuoteStatus.ACTIVE,
                )
            )
    return quotes


def parse_stream_frame(frame: dict[str, Any], provider: DataProvider) -> list[SportsbookQuote]:
    """Normalize one SSE frame.

    ``locked-odds`` covers suspension and removal without distinguishing them, so both map
    to a suspended quote. Treating a removal as a live price would be the dangerous error;
    treating a suspension as a removal only costs an opportunity.
    """
    event_type = str(frame.get("event", ""))
    if event_type not in {"odds", "locked-odds"}:
        return []

    status = QuoteStatus.SUSPENDED if event_type == "locked-odds" else QuoteStatus.ACTIVE
    data = frame.get("data")
    if not isinstance(data, list):
        raise ProviderDataError(f"stream frame '{event_type}' has no data list")

    quotes: list[SportsbookQuote] = []
    for odds in data:
        if not isinstance(odds, dict):
            raise ProviderDataError("stream odds entry must be an object")
        event_id = odds.get("fixture_id")
        if not isinstance(event_id, str) or not event_id:
            raise ProviderDataError("stream odds entry missing 'fixture_id'")
        quotes.append(
            _quote_from_odds(
                odds,
                provider,
                event_id,
                odds.get("game_id") if isinstance(odds.get("game_id"), str) else None,
                str(odds.get("sport", "")),
                str(odds.get("league", "")),
                status,
            )
        )
    return quotes
