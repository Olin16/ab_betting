"""Kalshi read-only REST client, entirely against MockTransport.

The client had no direct coverage: every prior test exercised its mapper through fixtures
while the request construction, error taxonomy and auth behaviour went untested. **No test
here makes a live request.**
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from event_arb.config import KalshiEnvironment, KalshiSettings
from event_arb.venues.base import (
    AuthenticationError,
    MarketDataError,
    RateLimitError,
    TransientError,
)
from event_arb.venues.kalshi.auth import KalshiSigner
from event_arb.venues.kalshi.client import KalshiClient

MARKET = {
    "ticker": "KX-TEST",
    "title": "Test market",
    "status": "active",
    "close_time": "2026-09-17T18:00:00Z",
    "rules_primary": "Resolves Yes if the condition is met.",
    "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0010"}],
}


def client_with(handler: object, **settings: object) -> KalshiClient:
    return KalshiClient(
        KalshiSettings(environment=KalshiEnvironment.PROD, **settings),  # type: ignore[arg-type]
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------- requests


async def test_discover_markets_requests_open_markets() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"markets": [MARKET]})

    markets = await client_with(handler).discover_markets()
    assert len(markets) == 1
    assert seen[0].url.path == "/trade-api/v2/markets"
    assert seen[0].url.params["status"] == "open"


async def test_discover_markets_applies_a_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"markets": []})

    await client_with(handler).discover_markets(limit=50)
    assert seen[0].url.params["limit"] == "50"


async def test_targeted_retrieval_fetches_each_ticker_individually() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"market": MARKET})

    await client_with(handler).discover_markets(market_ids=["A", "B"])
    assert paths == ["/trade-api/v2/markets/A", "/trade-api/v2/markets/B"]


async def test_orderbook_is_parsed_from_orderbook_fp() -> None:
    body = {"orderbook_fp": {"yes_dollars": [["0.5300", "120.00"]], "no_dollars": []}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trade-api/v2/markets/KX-TEST/orderbook"
        return httpx.Response(200, json=body)

    book = await client_with(handler).fetch_orderbook("KX-TEST")
    assert book.yes.bids[0].price == Decimal("0.53")


async def test_market_tick_comes_from_price_ranges() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": [MARKET]})

    markets = await client_with(handler).discover_markets()
    assert markets[0].tick_size == Decimal("0.0010")


# ------------------------------------------------------------- credentials


async def test_no_credentials_are_sent_when_none_are_configured() -> None:
    """Kalshi market data is public; the client must not invent auth headers."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"markets": []})

    await client_with(handler).discover_markets()
    headers = {k.lower() for k in seen[0].headers}
    assert "kalshi-access-key" not in headers
    assert "kalshi-access-signature" not in headers


async def test_requests_are_signed_when_credentials_are_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"markets": []})

    signer = KalshiSigner(
        api_key_id="test-key",
        private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )
    client = KalshiClient(
        KalshiSettings(environment=KalshiEnvironment.PROD),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        signer=signer,
    )
    await client.discover_markets()

    assert seen[0].headers["KALSHI-ACCESS-KEY"] == "test-key"
    assert seen[0].headers["KALSHI-ACCESS-SIGNATURE"]


# ------------------------------------------------------------- error taxonomy


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, RateLimitError),
        (500, TransientError),
        (503, TransientError),
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, MarketDataError),
        (400, MarketDataError),
    ],
)
async def test_http_status_maps_to_the_normalized_error(
    status: int, error: type[Exception]
) -> None:
    client = client_with(lambda request: httpx.Response(status))
    with pytest.raises(error):
        await client.fetch_orderbook("KX-TEST")


async def test_rate_limit_carries_retry_after() -> None:
    client = client_with(lambda request: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(RateLimitError) as info:
        await client.fetch_orderbook("KX-TEST")
    assert info.value.retry_after == 7.0


async def test_timeout_becomes_a_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientError):
        await client_with(handler).fetch_orderbook("KX-TEST")


async def test_transport_failure_becomes_a_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TransientError):
        await client_with(handler).discover_markets()


async def test_non_json_body_raises_market_data_error() -> None:
    client = client_with(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(MarketDataError):
        await client.fetch_orderbook("KX-TEST")


async def test_markets_payload_that_is_not_a_list_raises() -> None:
    client = client_with(lambda request: httpx.Response(200, json={"markets": "nope"}))
    with pytest.raises(MarketDataError):
        await client.discover_markets()


async def test_one_malformed_market_does_not_blind_the_scanner() -> None:
    """A single bad entry must not discard every other market in the page."""
    body = {"markets": [{"no_ticker": True}, MARKET]}
    markets = await client_with(lambda request: httpx.Response(200, json=body)).discover_markets()
    assert len(markets) == 1
    assert markets[0].venue_market_id == "KX-TEST"


async def test_targeted_retrieval_skips_a_failing_ticker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/BAD"):
            return httpx.Response(404)
        return httpx.Response(200, json={"market": MARKET})

    markets = await client_with(handler).discover_markets(market_ids=["BAD", "GOOD"])
    assert len(markets) == 1


# ------------------------------------------------------------------- safety


def test_client_exposes_no_order_entry_surface() -> None:
    forbidden = {"submit", "submit_order", "place_order", "create_order", "buy", "sell"}
    assert not (forbidden & set(dir(KalshiClient)))


async def test_health_records_successful_messages() -> None:
    client = client_with(lambda request: httpx.Response(200, json={"markets": []}))
    await client.discover_markets()
    assert client.health.messages_received >= 1


def test_environment_selects_the_documented_base_urls() -> None:
    assert KalshiSettings(environment=KalshiEnvironment.PROD).rest_base_url.startswith(
        "https://external-api.kalshi.com"
    )
    assert KalshiSettings(environment=KalshiEnvironment.DEMO).rest_base_url.startswith(
        "https://external-api.demo.kalshi.co"
    )


async def test_response_bodies_are_decoded_without_float_contamination() -> None:
    """Kalshi sends dollar strings, but a numeric field must not become a float."""
    body = json.dumps({"orderbook_fp": {"yes_dollars": [["0.5300", "120.00"]], "no_dollars": []}})
    client = client_with(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    )
    book = await client.fetch_orderbook("KX-TEST")
    assert isinstance(book.yes.bids[0].price, Decimal)


async def test_pagination_cursor_is_recorded_but_not_followed() -> None:
    """One page per authorisation. Following a cursor would exceed what was approved."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"cursor": "next-page-token", "markets": [MARKET]})

    client = client_with(handler)
    markets = await client.discover_markets(limit=10)

    assert calls["n"] == 1, "a cursor must not trigger a second request"
    assert len(markets) == 1
    assert client.last_pagination_cursor == "next-page-token"


async def test_absent_cursor_leaves_the_field_none() -> None:
    client = client_with(lambda request: httpx.Response(200, json={"markets": []}))
    await client.discover_markets()
    assert client.last_pagination_cursor is None


async def test_client_exposes_its_real_endpoint_and_auth_state() -> None:
    """The request planner reads these; they must not be aspirational."""
    client = client_with(lambda request: httpx.Response(200, json={"markets": []}))
    assert client.discovery_base_url.startswith("https://external-api.kalshi.com")
    assert client.sends_credentials is False
