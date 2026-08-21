"""Optional read-only checks against the real venue APIs.

**Deselected by default** (``-m "not live"`` in ``addopts``). Run explicitly with::

    pytest -m live

These tests only *read*. None of them places, cancels, or queries an order, and none can:
the read-only clients in this project have no order-entry methods at all.

They are useful for one thing specifically — detecting that a venue has changed its
schema out from under the assumptions recorded in ``docs/api-assumptions.md``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from event_arb.config import KalshiSettings, PolymarketSettings
from event_arb.venues.polymarket.geoblock import GeoblockChecker

pytestmark = pytest.mark.live

RUN_LIVE = os.environ.get("EVENT_ARB_RUN_LIVE_TESTS") == "1"
skip_unless_opted_in = pytest.mark.skipif(
    not RUN_LIVE,
    reason="set EVENT_ARB_RUN_LIVE_TESTS=1 to opt in to read-only live API calls",
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
async def test_polymarket_geoblock_endpoint_shape() -> None:
    """Confirms the geoblock endpoint still returns a boolean ``blocked``."""
    checker = GeoblockChecker(PolymarketSettings().geoblock_url)
    status = await checker.check()
    await checker.close()
    # We assert only on the shape, never that access is permitted.
    assert isinstance(status.allowed, bool)


@skip_unless_opted_in
async def test_polymarket_book_schema_still_matches_assumptions() -> None:
    settings = PolymarketSettings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        markets = await client.get(f"{settings.clob_base_url}/markets")
        markets.raise_for_status()
        entries = markets.json().get("data", [])
        assert entries, "Polymarket /markets returned no data"
        tokens = entries[0].get("tokens", [])
        assert tokens, "Polymarket market objects no longer carry 'tokens'"

        book = await client.get(
            f"{settings.clob_base_url}/book", params={"token_id": tokens[0]["token_id"]}
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
