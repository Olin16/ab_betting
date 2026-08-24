"""Optional read-only checks against the real venue APIs.

**Deselected by default** (``-m "not live"`` in ``addopts``). Run explicitly with::

    pytest -m live

These tests only *read*. None of them places, cancels, or queries an order, and none can:
the read-only clients in this project have no order-entry methods at all.

They are useful for one thing specifically — detecting that a venue has changed its
schema out from under the assumptions recorded in ``docs/api-assumptions.md``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import httpx
import pytest

from event_arb.config import KalshiSettings, PolymarketProduct, PolymarketSettings
from event_arb.venues.polymarket.geoblock import GeoblockChecker

pytestmark = pytest.mark.live

RUN_LIVE = os.environ.get("EVENT_ARB_RUN_LIVE_TESTS") == "1"
skip_unless_opted_in = pytest.mark.skipif(
    not RUN_LIVE,
    reason="set EVENT_ARB_RUN_LIVE_TESTS=1 to opt in to read-only live API calls",
)

GLOBAL_TERMS_CONFIRMED = (
    os.environ.get("EVENT_ARB_POLYMARKET__PRODUCT") == PolymarketProduct.GLOBAL.value
    and os.environ.get("EVENT_ARB_POLYMARKET__GLOBAL_TERMS_ELIGIBILITY_CONFIRMED", "").lower()
    == "true"
)
skip_unless_global_eligible = pytest.mark.skipif(
    not GLOBAL_TERMS_CONFIRMED,
    reason=(
        "Global schema checks require EVENT_ARB_POLYMARKET__PRODUCT=global and explicit "
        "Global terms eligibility confirmation"
    ),
)


def _global_settings() -> PolymarketSettings:
    return PolymarketSettings(
        product=PolymarketProduct.GLOBAL,
        global_terms_eligibility_confirmed=True,
    )


@skip_unless_opted_in
async def test_kalshi_orderbook_schema_still_matches_assumptions() -> None:
    """Confirms the orderbook still uses ``orderbook_fp`` with ``*_dollars`` arrays."""
    settings = KalshiSettings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        markets = await client.get(f"{settings.rest_base_url}/markets", params={"limit": 1})
        markets.raise_for_status()
        payload = markets.json()
        assert "markets" in payload, "Kalshi /markets response shape changed"
        ticker = payload["markets"][0]["ticker"]

        book = await client.get(f"{settings.rest_base_url}/markets/{ticker}/orderbook")
        book.raise_for_status()
        body = book.json()
        assert "orderbook_fp" in body, (
            "Kalshi orderbook no longer returns 'orderbook_fp'; "
            "docs/api-assumptions.md A2 needs updating"
        )


@skip_unless_opted_in
@skip_unless_global_eligible
async def test_polymarket_geoblock_endpoint_shape() -> None:
    """Confirms the Global geoblock endpoint still returns a boolean ``blocked``."""
    checker = GeoblockChecker(_global_settings().global_geoblock_url)
    status = await checker.check()
    await checker.close()
    # We assert only on the shape, never that access is permitted.
    assert isinstance(status.allowed, bool)


@skip_unless_opted_in
@skip_unless_global_eligible
async def test_polymarket_book_schema_still_matches_assumptions() -> None:
    settings = _global_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        markets = await client.get(f"{settings.global_clob_base_url}/markets")
        markets.raise_for_status()
        entries = markets.json().get("data", [])
        assert entries, "Polymarket /markets returned no data"
        tokens = entries[0].get("tokens", [])
        assert tokens, "Polymarket market objects no longer carry 'tokens'"

        book = await client.get(
            f"{settings.global_clob_base_url}/book",
            params={"token_id": tokens[0]["token_id"]},
        )
        book.raise_for_status()
        body = book.json()
        assert "bids" in body and "asks" in body, "Polymarket /book response shape changed"


def test_no_live_test_can_place_an_order() -> None:
    """Structural guarantee: the read-only clients expose no order-entry surface."""
    from event_arb.venues.kalshi.client import KalshiClient
    from event_arb.venues.polymarket.client import PolymarketClient

    forbidden = {"submit", "submit_order", "place_order", "create_order", "buy", "sell", "cancel"}
    for client_class in (KalshiClient, PolymarketClient):
        assert not (forbidden & set(dir(client_class))), (
            f"{client_class.__name__} exposes an order-entry method"
        )


# --------------------------------------------------------------- Polymarket US schema


@skip_unless_opted_in
async def test_polymarket_us_public_market_schema_still_matches_assumptions() -> None:
    """Guards the corrections recorded in docs/api-assumptions.md for the US gateway.

    Each of these was found by reading a live payload and each, if it silently changed,
    would be mispriced rather than rejected:

    - ``feeCoefficient`` per market — the US fee model is theta * C * p * (1-p), so losing
      the published coefficient means falling back to a default that may not apply;
    - ``outcomes`` as a JSON-encoded *string* — read undecoded it iterates characters;
    - ``orderPriceMinTickSize`` finer than a penny — assuming 0.01 rounds real prices away;
    - no ``resolutionSource`` — the absence is load-bearing, because approval requires a
      named settlement authority and this venue does not publish one.
    """
    settings = PolymarketSettings(product=PolymarketProduct.US)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.us_gateway_url}/v1/markets",
            params={"active": "true", "closed": "false", "limit": 1},
        )

    assert response.status_code == 200, "the public gateway should need no credential"
    payload = response.json()
    markets = payload["markets"] if isinstance(payload, dict) else payload
    assert isinstance(markets, list) and markets, "expected at least one open market"
    market = markets[0]

    assert "feeCoefficient" in market, "per-market fee coefficient disappeared"
    assert Decimal(str(market["feeCoefficient"])) >= 0

    assert isinstance(market["outcomes"], str), (
        "outcomes is documented as a JSON-encoded string; a list here means the shape "
        "changed and the mapper's json.loads would now fail"
    )
    assert json.loads(market["outcomes"]), "outcomes decoded to nothing"

    tick = Decimal(str(market["orderPriceMinTickSize"]))
    assert tick > 0, "a non-positive tick would make depth walking meaningless"

    assert "resolutionSource" not in market, (
        "the US gateway began publishing a settlement source; approval readiness treats "
        "its absence as a blocker, so this is a change worth acting on"
    )

    assert str(market["status"]).startswith("MARKET_STATUS_"), "status enum shape changed"
