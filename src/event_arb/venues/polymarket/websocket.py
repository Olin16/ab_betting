"""Polymarket market-channel WebSocket stream.

Protocol (verified 2026-08-21, docs.polymarket.com/api-reference/wss/market)::

    URL:       wss://ws-subscriptions-clob.polymarket.com/ws/market
    subscribe: {"assets_ids": [...], "type": "market", "initial_dump": true, "level": 2}
    events:    book | price_change | tick_size_change | last_trade_price
    keepalive: send "PING" every 10 seconds

**No sequence numbers.** Unlike Kalshi, this channel carries only a ``hash`` per book.
There is therefore no way to prove that no update was missed. Two mitigations are
applied, and every Polymarket book carries the
:attr:`~event_arb.domain.enums.RiskFlag.NO_SEQUENCE_NUMBERS` flag downstream so the
weakness is visible rather than assumed away:

1. a full ``book`` snapshot replaces local state outright whenever one arrives;
2. state is re-requested from REST on a periodic interval by the synchronizer.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from event_arb.config import PolymarketSettings
from event_arb.domain.enums import Outcome, Venue
from event_arb.domain.models import BookLevel, OrderBook, OrderBookSide
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.venues.base import AdapterHealth, BackoffPolicy, StreamEvent

__all__ = ["PolymarketBookState", "PolymarketWebSocketStream"]

log = get_logger(__name__)

PING_INTERVAL_SECONDS = 10.0


class PolymarketBookState:
    """Per-token bid/ask maps for one market, keyed by outcome."""

    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        self.bids: dict[Outcome, dict[Decimal, Decimal]] = {Outcome.YES: {}, Outcome.NO: {}}
        self.asks: dict[Outcome, dict[Decimal, Decimal]] = {Outcome.YES: {}, Outcome.NO: {}}
        self.exchange_timestamp: datetime | None = None
        self.tick_size: Decimal | None = None
        self.seen_snapshot = False

    def apply_book(self, outcome: Outcome, msg: dict[str, Any]) -> None:
        """Replace one outcome's book wholesale from a ``book`` snapshot."""
        self.bids[outcome] = _to_map(msg.get("bids"))
        self.asks[outcome] = _to_map(msg.get("asks"))
        self.seen_snapshot = True
        ts = msg.get("timestamp")
        if ts is not None:
            self.exchange_timestamp = _parse_ms(ts)

    def apply_price_change(self, outcome: Outcome, change: dict[str, Any]) -> None:
        """Apply one absolute price-level update.

        ``price_change`` carries the *new size* at a level, not a delta: a size of zero
        removes the level.
        """
        try:
            price = Decimal(str(change["price"]))
            size = Decimal(str(change["size"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"malformed Polymarket price_change: {change!r}") from exc

        side_name = str(change.get("side", "")).upper()
        if side_name == "BUY":
            target = self.bids[outcome]
        elif side_name == "SELL":
            target = self.asks[outcome]
        else:
            raise ValueError(f"unknown Polymarket side: {side_name!r}")

        if size <= 0:
            target.pop(price, None)
        else:
            target[price] = size

    def to_book(self) -> OrderBook:
        def side(outcome: Outcome) -> OrderBookSide:
            bids = sorted(self.bids[outcome].items(), key=lambda kv: kv[0], reverse=True)
            asks = sorted(self.asks[outcome].items(), key=lambda kv: kv[0])
            return OrderBookSide(
                outcome=outcome,
                bids=tuple(BookLevel(price=p, quantity=q) for p, q in bids),
                asks=tuple(BookLevel(price=p, quantity=q) for p, q in asks),
            )

        yes = side(Outcome.YES)
        no = side(Outcome.NO)
        return OrderBook(
            venue=Venue.POLYMARKET,
            market_id=self.market_id,
            yes=yes,
            no=no,
            exchange_timestamp=self.exchange_timestamp,
            received_at=datetime.now(UTC),
            sequence=None,
            is_complete=self.seen_snapshot and bool(yes.bids or yes.asks or no.bids or no.asks),
        )


def _parse_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(str(value)) / 1000.0, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _to_map(raw: Any) -> dict[Decimal, Decimal]:
    result: dict[Decimal, Decimal] = {}
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            price = Decimal(str(entry["price"]))
            size = Decimal(str(entry["size"]))
        except (KeyError, InvalidOperation, ValueError):
            continue
        if size > 0:
            result[price] = size
    return result


class PolymarketWebSocketStream:
    """Reconnecting Polymarket market-channel stream."""

    def __init__(
        self,
        settings: PolymarketSettings,
        health: AdapterHealth,
        token_map: dict[str, dict[str, str]],
        backoff: BackoffPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._health = health
        self._backoff = backoff or BackoffPolicy()
        self._rng = rng
        self._states: dict[str, PolymarketBookState] = {}
        #: asset/token id -> (market_id, outcome)
        self._asset_index: dict[str, tuple[str, Outcome]] = {}
        for market_id, tokens in token_map.items():
            for outcome_name, token_id in tokens.items():
                try:
                    outcome = Outcome(outcome_name.upper())
                except ValueError:
                    continue
                self._asset_index[token_id] = (market_id, outcome)

    def _subscribe_payload(self) -> str:
        return json.dumps(
            {
                "assets_ids": sorted(self._asset_index),
                "type": "market",
                "initial_dump": True,
                "level": 2,
            }
        )

    async def run(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        attempt = 0
        while True:
            ping_task: asyncio.Task[None] | None = None
            try:
                async with websockets.connect(self._settings.ws_url) as connection:
                    attempt = 0
                    self._health.connected = True
                    self._health.subscribed_markets = set(market_ids)
                    await connection.send(self._subscribe_payload())
                    ping_task = asyncio.create_task(self._keepalive(connection))
                    async for event in self._consume(connection):
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # normalize every transport failure into a reconnect
                self._health.connected = False
                self._health.record_error(str(exc))
                self._health.reconnect_count += 1
                metrics.ws_reconnects.labels(venue=Venue.POLYMARKET.value).inc()
                attempt += 1
                delay = self._backoff.delay_for(attempt, self._rng)
                log.warning(
                    "polymarket.ws.reconnect",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
                # Without sequence numbers, a reconnect invalidates all local state.
                self._states.clear()
                await asyncio.sleep(delay)
            finally:
                if ping_task is not None:
                    ping_task.cancel()

    async def _keepalive(self, connection: ClientConnection) -> None:
        """Send the documented literal ``PING`` every 10 seconds."""
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            await connection.send("PING")

    async def _consume(self, connection: ClientConnection) -> AsyncIterator[StreamEvent]:
        async for raw in connection:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw.strip() in {"PONG", "PING"}:
                continue
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("polymarket.ws.malformed_json")
                continue

            # The channel may deliver either a single event or a batch.
            events = message if isinstance(message, list) else [message]
            for entry in events:
                if not isinstance(entry, dict):
                    continue
                produced = self._handle(entry)
                if produced is not None:
                    yield produced

    def _resolve(self, asset_id: Any) -> tuple[str, Outcome] | None:
        if not isinstance(asset_id, str):
            return None
        return self._asset_index.get(asset_id)

    def _handle(self, message: dict[str, Any]) -> StreamEvent | None:
        event_type = message.get("event_type")
        self._health.record_message()

        if event_type == "book":
            resolved = self._resolve(message.get("asset_id"))
            if resolved is None:
                return None
            market_id, outcome = resolved
            state = self._states.setdefault(market_id, PolymarketBookState(market_id))
            state.apply_book(outcome, message)
            metrics.books_received.labels(venue=Venue.POLYMARKET.value).inc()
            return StreamEvent(Venue.POLYMARKET, market_id, state.to_book(), is_snapshot=True)

        if event_type == "price_change":
            changes = message.get("price_changes")
            if not isinstance(changes, list):
                return None
            touched: PolymarketBookState | None = None
            corrupted = False
            for change in changes:
                if not isinstance(change, dict):
                    continue
                resolved = self._resolve(change.get("asset_id"))
                if resolved is None:
                    continue
                market_id, outcome = resolved
                pending = self._states.get(market_id)
                if pending is None or not pending.seen_snapshot:
                    # Applying an update to a book we never snapshotted would silently
                    # build a partial book that looks complete.
                    continue
                try:
                    pending.apply_price_change(outcome, change)
                except ValueError as exc:
                    # An update we could not apply means the book is now missing a change
                    # the venue has already made. Skipping it silently would leave a
                    # divergent book eligible for opportunity detection.
                    log.warning("polymarket.ws.bad_price_change", error=str(exc))
                    corrupted = True
                    touched = pending
                    continue
                touched = pending
            if touched is None:
                return None
            ts = message.get("timestamp")
            if ts is not None:
                touched.exchange_timestamp = _parse_ms(ts)
            metrics.books_received.labels(venue=Venue.POLYMARKET.value).inc()

            book = touched.to_book()
            if corrupted:
                # Without sequence numbers there is no resync command to send; the
                # periodic REST resynchronization will repair it. Until then the book
                # must not be treated as usable.
                touched.seen_snapshot = False
                metrics.resyncs.labels(venue=Venue.POLYMARKET.value).inc()
                book = book.model_copy(update={"is_stale": True})
            return StreamEvent(Venue.POLYMARKET, touched.market_id, book, is_snapshot=False)

        if event_type == "tick_size_change":
            resolved = self._resolve(message.get("asset_id"))
            if resolved is not None:
                market_id, _ = resolved
                existing = self._states.get(market_id)
                if existing is not None:
                    with suppress(InvalidOperation, ValueError):
                        existing.tick_size = Decimal(str(message.get("new_tick_size")))
                log.info("polymarket.ws.tick_size_change", market_id=market_id)
            return None

        return None
