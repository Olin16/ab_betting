"""Shared test fixtures.

Every test in this suite runs against recorded fixtures. No test may contact a live
venue API or place an order of any kind; the ``live`` marker gates the only optional
read-only exception and is deselected by default.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def no_live_venue_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly rather than silently reaching a venue.

    A test that constructs ``Settings(data_source=LIVE)`` gets real venue clients, and
    ``discover()`` on those clients issues real HTTP requests. That happened during
    development: an integration test meant to exercise the paper re-fetch path built live
    adapters and hit Kalshi and Polymarket for real. Nothing was authenticated and no order
    was placed, but the suite must not be able to reach a venue at all.

    Only the *real* transport is blocked. ``httpx.MockTransport`` does not go through it,
    so the many client tests that serve canned responses keep working — which is the point:
    the guard must stop real sockets without pushing tests away from exercising the clients.
    """
    import httpx

    async def refuse(self: object, *args: object, **kwargs: object) -> None:
        raise LiveNetworkError(
            "a test attempted a real HTTP request to a venue. Use fixture adapters or "
            "httpx.MockTransport; never let the suite reach a live venue."
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse, raising=True)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse, raising=True)
