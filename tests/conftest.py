"""Shared test fixtures.

Every test in this suite runs against recorded fixtures. No test may contact a live
venue API or place an order of any kind; the ``live`` marker gates the only optional
read-only exception and is deselected by default.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from event_arb.config import PolymarketProduct, PolymarketSettings, Settings
from event_arb.domain.enums import Venue
from event_arb.venues.fixture import FixtureAdapter

FIXTURE_DIR = Path(__file__).parent / "fixtures"

KALSHI_FED = "KXFEDDECISION-26SEP-CUT25"
KALSHI_NYC = "KXHIGHNY-26AUG22-B90"
KALSHI_CPI = "KXCPIYOY-26SEP-T3.0"
POLY_FED = "0xfed26sepcut25aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
POLY_NYC = "0xnychigh26aug22bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
POLY_CPI = "0xcpiyoy26augcccccccccccccccccccccccccccccccccccccccccccccccccc"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def kalshi_adapter() -> FixtureAdapter:
    return FixtureAdapter(Venue.KALSHI, FIXTURE_DIR)


@pytest.fixture
def polymarket_adapter() -> FixtureAdapter:
    return FixtureAdapter(Venue.POLYMARKET, FIXTURE_DIR)


@pytest.fixture
def settings() -> Settings:
    # Legacy Polymarket **Global** corpus; the US path has its own settings fixture.
    return Settings(
        fixture_dir=FIXTURE_DIR,
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
    )


@pytest.fixture
def us_settings() -> Settings:
    """Settings selecting the Polymarket **US** product and its fixture corpus."""
    return Settings(
        fixture_dir=FIXTURE_DIR,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )


class LiveNetworkError(AssertionError):
    """Raised when a test tries to open a real connection to a venue."""


#: Read-only HTTP. A live test may look at a venue; it may never change anything there.
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _live_access_allowed(request: pytest.FixtureRequest) -> bool:
    """Network is permitted only for a test that is explicitly, deliberately live.

    Both conditions are required and neither implies the other. The marker says the author
    intended this test to reach a venue; the environment variable says the *operator*
    intended it on this run. A marker alone would let ``pytest -m live`` fire on a machine
    that never asked for it, and the variable alone would open the network to every test.
    """
    if os.environ.get("EVENT_ARB_RUN_LIVE_TESTS") != "1":
        return False
    return request.node.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def no_live_venue_calls(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fail loudly rather than silently reaching a venue.

    A test that constructs ``Settings(data_source=LIVE)`` gets real venue clients, and
    ``discover()`` on those clients issues real HTTP requests. That happened during
    development: an integration test meant to exercise the paper re-fetch path built live
    adapters and hit Kalshi and Polymarket for real.

    Ordinary tests are fail-closed. A test carrying the ``live`` marker, on a run where
    ``EVENT_ARB_RUN_LIVE_TESTS=1``, is allowed out — but only for read-only methods, so an
    opted-in test still cannot POST, PUT, PATCH or DELETE against a venue.

    Only the *real* transports are patched. ``httpx.MockTransport`` does not go through
    them, so the many client tests that serve canned responses keep working — the guard
    must stop real sockets without pushing tests away from exercising the clients.
    """
    import httpx

    if _live_access_allowed(request):
        _restrict_to_read_only(monkeypatch)
        return

    async def refuse_async(self: object, *args: object, **kwargs: object) -> None:
        raise LiveNetworkError(
            "a test attempted a real HTTP request to a venue. Use fixture adapters or "
            "httpx.MockTransport; never let the suite reach a live venue."
        )

    def refuse_sync(self: object, *args: object, **kwargs: object) -> None:
        raise LiveNetworkError(
            "a test attempted a real HTTP request to a venue. Use fixture adapters or "
            "httpx.MockTransport; never let the suite reach a live venue."
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_sync)
    _block_raw_sockets(monkeypatch)


def _restrict_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let an opted-in live test read, and only read.

    The read-only clients in this project have no order-entry methods, so this is a second
    line rather than the only one — but "the client cannot" and "the transport will not"
    fail differently, and a schema check that grows a POST should fail here rather than at
    the venue.
    """
    import httpx

    original_async = httpx.AsyncHTTPTransport.handle_async_request
    original_sync = httpx.HTTPTransport.handle_request

    async def guarded_async(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        _require_read_only(request)
        return await original_async(self, request)

    def guarded_sync(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        _require_read_only(request)
        return original_sync(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)


def _require_read_only(request: object) -> None:
    method = str(getattr(request, "method", "")).upper()
    if method not in _READ_ONLY_METHODS:
        raise LiveNetworkError(
            f"live tests are read-only; refusing a {method} request to "
            f"{getattr(request, 'url', '<unknown>')}"
        )


def _block_raw_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the door httpx is not standing in.

    WebSocket clients and anything else that opens a socket directly bypass the httpx
    transport entirely, so patching httpx alone left a way out of the suite.
    """
    import socket

    def refuse_connect(self: object, address: object) -> None:
        raise LiveNetworkError(
            f"a test attempted a raw socket connection to {address!r}. "
            "Ordinary tests must not touch the network."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect)
