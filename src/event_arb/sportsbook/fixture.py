"""Fixture-backed odds feed.

The default and, until a provider licence is verified, the *only* source of sportsbook
data in this project. It implements the same :class:`OddsFeedAdapter` contract as a live
client would and drives the same provider mappers, so fixtures exercise real parsing code
rather than a parallel test-only path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from event_arb.sportsbook.base import OddsFeedAdapter, ProviderDataError, QuoteUpdate
from event_arb.sportsbook.enums import DataProvider, QuoteStatus
from event_arb.sportsbook.models import SportsbookEvent, SportsbookQuote

# OddsJam and OpticOdds share one API surface, so they share one mapper.
# See docs/sportsbook-data-plan.md.
from event_arb.sportsbook.providers import opticodds

__all__ = ["FixtureOddsFeed", "load_provider_json"]


def load_provider_json(path: Path) -> Any:
    """Load a provider payload, keeping every number exact.

    ``parse_float=Decimal`` is the whole point. Provider odds and lines arrive as JSON
    numbers, and a stock decoder turns them into binary floats before any of our code sees
    them — which would defeat the Decimal discipline at the moment of ingestion, in the one
    place it cannot be recovered.
    """
    try:
        return json.loads(path.read_text(), parse_float=Decimal)
    except FileNotFoundError as exc:
        raise ProviderDataError(f"fixture not found: {path}") from exc
    except ValueError as exc:
        raise ProviderDataError(f"invalid JSON fixture: {path}") from exc


class FixtureOddsFeed(OddsFeedAdapter):
    """Replays recorded or synthetic provider payloads."""

    def __init__(
        self,
        provider: DataProvider,
        fixture_dir: Path,
        frame_interval: float = 0.0,
    ) -> None:
        super().__init__(provider)
        self._dir = fixture_dir / provider.value
        self._frame_interval = frame_interval

    # ------------------------------------------------------------------- internals

    def _load(self, name: str) -> Any:
        return load_provider_json(self._dir / name)

    # --------------------------------------------------------------------- adapter

    async def discover_events(
        self, sport: str | None = None, league: str | None = None
    ) -> list[SportsbookEvent]:
        payload = self._load("fixtures.json")
        events = opticodds.parse_events(payload, self.provider)
        if sport:
            events = [e for e in events if e.sport.lower() == sport.lower()]
        if league:
            events = [e for e in events if e.league.lower() == league.lower()]
        self.health.record_message()
        self.health.connected = True
        return events

    async def fetch_quotes(
        self, event_ids: list[str], sportsbooks: list[str] | None = None
    ) -> list[SportsbookQuote]:
        payload = self._load("odds.json")
        quotes = opticodds.parse_quotes(payload, self.provider)
        wanted = set(event_ids)
        if wanted:
            quotes = [q for q in quotes if q.provider_event_id in wanted]
        if sportsbooks:
            books = {b.lower() for b in sportsbooks}
            quotes = [q for q in quotes if q.sportsbook.lower() in books]
        self.health.record_message()
        self.health.connected = True
        return quotes

    async def stream(
        self, sports: list[str], sportsbooks: list[str] | None = None
    ) -> AsyncIterator[QuoteUpdate]:
        """Replay recorded stream frames in order."""
        self.health.connected = True
        self.health.subscribed_sports = set(sports)
        frames = self._load("stream.json")
        if not isinstance(frames, list):
            raise ProviderDataError("stream fixture must be a list of SSE frames")

        for frame in frames:
            if not isinstance(frame, dict):
                continue
            if self._frame_interval:
                await asyncio.sleep(self._frame_interval)

            event_type = str(frame.get("event", ""))
            if event_type in {"connected", "ping"}:
                self.health.record_message()
                continue

            try:
                updates = opticodds.parse_stream_frame(frame, self.provider)
            except ProviderDataError:
                # A malformed frame is recorded, not swallowed: it means we are missing an
                # update the book has already made, so downstream must treat state as
                # suspect rather than current.
                self.health.malformed_messages += 1
                self.health.record_error(f"malformed stream frame: {event_type}")
                continue

            entry_id = frame.get("entry_id")
            if isinstance(entry_id, str):
                self.health.last_entry_id = entry_id

            for quote in updates:
                self.health.record_message()
                yield QuoteUpdate(
                    provider=self.provider,
                    quote=quote,
                    entry_id=self.health.last_entry_id,
                    is_suspension=quote.status is QuoteStatus.SUSPENDED,
                )
