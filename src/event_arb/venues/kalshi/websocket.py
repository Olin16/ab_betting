"""Kalshi WebSocket stream with sequence-gap detection and snapshot resync.

Protocol (verified 2026-08-21, docs.kalshi.com/websockets/orderbook-updates)::

    {"type": "orderbook_snapshot", "sid": 2, "seq": 2,
     "msg": {"market_ticker": ..., "yes_dollars_fp": [...], "no_dollars_fp": [...]}}

    {"type": "orderbook_delta", "sid": 2, "seq": 3,
     "msg": {"market_ticker": ..., "price_dollars": "0.960", "delta_fp": "-54.00",
             "side": "yes", "ts_ms": 1669149841000}}

The channel delivers one snapshot followed by deltas. Every delta must be applied in
order; a missed message silently corrupts the book, and a corrupt book produces a
phantom arbitrage. So any ``seq`` that is not exactly ``previous + 1`` is treated as a
gap: the affected book is marked stale immediately and a fresh snapshot is requested via
``update_subscription`` / ``get_snapshot``. Stale books are suppressed by the opportunity
engine, so the failure mode is a missed opportunity rather than a fabricated one.

**Sequence numbers are tracked per subscription (``sid``), one subscription per market.**

The documentation attaches ``seq`` to the subscription, but a single subscription may
name several markets — which leaves it ambiguous whether ``seq`` advances per market or
across the whole subscription. Tracking per market under a shared subscription would
manufacture a gap on every interleaved update if the counter is subscription-wide; the
reverse assumption would silently miss real gaps.

Rather than guess, each market gets its **own subscription command**, multiplexed over a
single connection (Kalshi accepts multiple subscribe commands per connection, so this
costs no extra sockets). Then ``sid`` identifies exactly one market and ``seq`` is
unambiguous under either interpretation. See docs/api-assumptions.md A6.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from event_arb.config import KalshiSettings
from event_arb.domain.enums import Venue
from event_arb.domain.models import OrderBook
from event_arb.observability import metrics
from event_arb.observability.logging import get_logger
from event_arb.venues.base import AdapterHealth, BackoffPolicy, StreamEvent
from event_arb.venues.kalshi.auth import KalshiSigner
from event_arb.venues.kalshi.mapper import build_book, parse_orderbook

__all__ = ["KalshiBookState", "KalshiWebSocketStream"]

log = get_logger(__name__)

_WS_PATH = "/trade-api/ws/v2"


class KalshiBookState:
    """Maintains one market's book across a snapshot plus a stream of deltas.

    Levels are held as ``{price: quantity}`` maps per side because deltas address a
    single price point and may remove it entirely (quantity falling to zero).
    """

    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        self.yes: dict[Decimal, Decimal] = {}
        self.no: dict[Decimal, Decimal] = {}
        self.last_seq: int | None = None
        self.needs_resync: bool = True
        self.exchange_timestamp: datetime | None = None

    def apply_snapshot(self, msg: dict[str, Any], seq: int | None) -> OrderBook:
        book = parse_orderbook(self.market_id, msg, received_at=datetime.now(UTC), sequence=seq)
        self.yes = {level.price: level.quantity for level in book.yes.bids}
        self.no = {level.price: level.quantity for level in book.no.bids}
        self.last_seq = seq
        self.needs_resync = False
        return book

    def check_sequence(self, seq: int | None) -> bool:
        """Return True when ``seq`` follows the previous one; False marks a gap."""
        if seq is None or self.last_seq is None:
            return True
        return seq == self.last_seq + 1

    def apply_delta(self, msg: dict[str, Any], seq: int | None) -> OrderBook:
        """Apply one price-level delta and rebuild the normalized book."""
        try:
            price = Decimal(str(msg["price_dollars"]))
            delta = Decimal(str(msg["delta_fp"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"malformed Kalshi delta: {msg!r}") from exc

        side_name = str(msg.get("side", "")).lower()
        side = self.yes if side_name == "yes" else self.no
        if side_name not in {"yes", "no"}:
            raise ValueError(f"unknown Kalshi delta side: {side_name!r}")

        updated = side.get(price, Decimal("0")) + delta
        if updated <= 0:
            side.pop(price, None)
        else:
            side[price] = updated

        self.last_seq = seq
        ts_ms = msg.get("ts_ms")
        if isinstance(ts_ms, (int, float)):
            self.exchange_timestamp = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=UTC)
        return self.to_book(seq)

    def to_book(self, seq: int | None) -> OrderBook:
        return build_book(
            market_id=self.market_id,
            yes_bids=list(self.yes.items()),
            no_bids=list(self.no.items()),
            received_at=datetime.now(UTC),
            sequence=seq,
            exchange_timestamp=self.exchange_timestamp,
        )


class KalshiWebSocketStream:
    """Reconnecting Kalshi orderbook stream."""

    def __init__(
        self,
        settings: KalshiSettings,
        health: AdapterHealth,
        signer: KalshiSigner | None = None,
        backoff: BackoffPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._health = health
        self._signer = signer
        self._backoff = backoff or BackoffPolicy()
        self._rng = rng
        self._command_id = 0
        #: sid -> book state. One subscription per market, so a sid names one market.
        self._states: dict[int, KalshiBookState] = {}
        #: command id -> market, so a subscription confirmation can be bound to its market.
        self._pending: dict[int, str] = {}
        #: market -> sid, for targeted resync requests.
        self._sids: dict[str, int] = {}

    def _next_id(self) -> int:
        self._command_id += 1
        return self._command_id

    def _subscribe_payload(self, market_id: str) -> str:
        """One subscription per market, so ``sid`` unambiguously identifies a market."""
        command_id = self._next_id()
        self._pending[command_id] = market_id
        return json.dumps(
            {
                "id": command_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": [market_id],
                    # Pin the price convention rather than inheriting a default Kalshi
                    # has said may change; a silent flip would invert NO-side prices.
                    "use_yes_price": True,
                },
            }
        )

    def _resync_payload(self, market_id: str) -> str:
        """Ask for a fresh snapshot without disturbing the subscription."""
        params: dict[str, Any] = {
            "action": "get_snapshot",
            "market_tickers": [market_id],
        }
        sid = self._sids.get(market_id)
        if sid is not None:
            params["sid"] = sid
        return json.dumps({"id": self._next_id(), "cmd": "update_subscription", "params": params})

    async def run(self, market_ids: list[str]) -> AsyncIterator[StreamEvent]:
        """Yield normalized book updates, reconnecting with jittered backoff."""
        attempt = 0
        while True:
            try:
                headers = self._signer.headers("GET", _WS_PATH) if self._signer is not None else {}
                async with websockets.connect(
                    self._settings.ws_url, additional_headers=headers
                ) as connection:
                    attempt = 0
                    self._health.connected = True
                    self._health.subscribed_markets = set(market_ids)
                    # One subscription command per market, multiplexed on one socket.
                    self._pending.clear()
                    self._sids.clear()
                    self._states.clear()
                    for market_id in market_ids:
                        await connection.send(self._subscribe_payload(market_id))
                    async for event in self._consume(connection, market_ids):
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # normalize every transport failure into a reconnect
                self._health.connected = False
                self._health.record_error(str(exc))
                self._health.reconnect_count += 1
                metrics.ws_reconnects.labels(venue=Venue.KALSHI.value).inc()
                attempt += 1
                delay = self._backoff.delay_for(attempt, self._rng)
                log.warning(
                    "kalshi.ws.reconnect",
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
                # Every book is untrustworthy across a disconnect.
                for state in self._states.values():
                    state.needs_resync = True
                await asyncio.sleep(delay)

    async def _consume(
        self, connection: ClientConnection, market_ids: list[str]
    ) -> AsyncIterator[StreamEvent]:
        async for raw in connection:
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("kalshi.ws.malformed_json")
                continue
            if not isinstance(message, dict):
                continue

            event = await self._handle(message, connection, market_ids)
            if event is not None:
                yield event

    async def _handle(
        self, message: dict[str, Any], connection: ClientConnection, market_ids: list[str]
    ) -> StreamEvent | None:
        msg_type = message.get("type")

        # Bind a subscription confirmation to the market whose command produced it.
        if msg_type in {"subscribed", "ok"}:
            self._bind_subscription(message)
            return None

        payload = message.get("msg")
        if msg_type == "error" or not isinstance(payload, dict):
            if msg_type == "error":
                self._health.record_error(str(payload))
                log.warning("kalshi.ws.error_message")
            return None

        sid = message.get("sid")
        if not isinstance(sid, int):
            return None

        state = self._states.get(sid)
        if state is None:
            # A sid we never bound: fall back to the ticker on the message itself so a
            # confirmation we missed cannot silently drop the whole market's feed.
            market_id = payload.get("market_ticker")
            if not isinstance(market_id, str):
                return None
            state = KalshiBookState(market_id)
            self._states[sid] = state
            self._sids[market_id] = sid

        seq = message.get("seq")
        seq_int = seq if isinstance(seq, int) else None
        self._health.record_message()
        metrics.books_received.labels(venue=Venue.KALSHI.value).inc()

        if msg_type == "orderbook_snapshot":
            book = state.apply_snapshot(payload, seq_int)
            return StreamEvent(Venue.KALSHI, state.market_id, book, is_snapshot=True)

        if msg_type != "orderbook_delta":
            return None

        if state.needs_resync:
            return None

        if not state.check_sequence(seq_int):
            self._health.sequence_gaps += 1
            self._health.resync_count += 1
            metrics.resyncs.labels(venue=Venue.KALSHI.value).inc()
            log.warning(
                "kalshi.ws.sequence_gap",
                market_id=state.market_id,
                sid=sid,
                expected=(state.last_seq or 0) + 1,
                received=seq_int,
            )
            state.needs_resync = True
            stale = state.to_book(seq_int).model_copy(update={"is_stale": True})
            await connection.send(self._resync_payload(state.market_id))
            return StreamEvent(Venue.KALSHI, state.market_id, stale, is_snapshot=False)

        try:
            book = state.apply_delta(payload, seq_int)
        except ValueError as exc:
            log.warning("kalshi.ws.bad_delta", market_id=state.market_id, error=str(exc))
            state.needs_resync = True
            # Emit a stale book rather than swallowing the failure. A malformed delta
            # means an update we could not apply, so the stored book is now missing a
            # change the venue has already made — exactly as damaging as a sequence gap.
            # Returning None would leave the previous book eligible for detection until
            # its freshness timer happened to expire.
            stale = state.to_book(seq_int).model_copy(update={"is_stale": True})
            await connection.send(self._resync_payload(state.market_id))
            return StreamEvent(Venue.KALSHI, state.market_id, stale, is_snapshot=False)
        return StreamEvent(Venue.KALSHI, state.market_id, book, is_snapshot=False)

    def _bind_subscription(self, message: dict[str, Any]) -> None:
        """Associate the sid Kalshi assigned with the market we subscribed for."""
        command_id = message.get("id")
        sid = message.get("sid")
        if sid is None:
            inner = message.get("msg")
            if isinstance(inner, dict):
                sid = inner.get("sid")
        if not isinstance(sid, int):
            return

        market_id = self._pending.pop(command_id, None) if isinstance(command_id, int) else None
        if market_id is None:
            return

        self._sids[market_id] = sid
        self._states[sid] = KalshiBookState(market_id)
        log.info("kalshi.ws.subscribed", market_id=market_id, sid=sid)
