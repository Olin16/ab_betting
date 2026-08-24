"""A 429 must stop us, not merely slow us.

RateLimitError has always carried the venue's Retry-After, and its docstring claimed the
value "is always honoured". Nothing honoured it. The token bucket caps our rate but does
not stop us, so after a 429 the scanner kept issuing at the configured rate for the whole
cooldown the venue had just asked us to observe.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from event_arb.config import KalshiSettings, PolymarketProduct, PolymarketSettings
from event_arb.venues.base import RateLimiter, RateLimitError
from event_arb.venues.kalshi.client import KalshiClient
from event_arb.venues.polymarket_us.client import PolymarketUSClient

pytestmark = pytest.mark.anyio


async def test_pause_for_blocks_acquire_until_the_deadline() -> None:
    limiter = RateLimiter(rate_per_second=1000.0)
    loop = asyncio.get_running_loop()

    limiter.pause_for(0.25)
    assert limiter.paused is True

    started = loop.time()
    await limiter.acquire()
    assert loop.time() - started >= 0.2, "acquire returned before the pause elapsed"
    assert limiter.paused is False


async def test_a_longer_pause_never_shortens_an_existing_one() -> None:
    """Two 429s in flight must not let the second one shorten the first one's cooldown."""
    limiter = RateLimiter(rate_per_second=1000.0)
    limiter.pause_for(10.0)
    limiter.pause_for(0.01)

    loop = asyncio.get_running_loop()
    assert limiter._paused_until - loop.time() > 5.0


async def test_a_non_positive_pause_is_ignored() -> None:
    limiter = RateLimiter(rate_per_second=1000.0)
    limiter.pause_for(0)
    limiter.pause_for(-5)
    assert limiter.paused is False


def _429(retry_after: str | None) -> httpx.MockTransport:
    headers = {"Retry-After": retry_after} if retry_after else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers, json={})

    return httpx.MockTransport(handler)


async def test_kalshi_honours_retry_after() -> None:
    client = KalshiClient(KalshiSettings())
    client._client = httpx.AsyncClient(transport=_429("45"))

    with pytest.raises(RateLimitError) as caught:
        await client.discover_markets(limit=1)

    assert caught.value.retry_after == 45.0
    assert client._limiter.paused is True
    await client.close()


async def test_polymarket_us_honours_retry_after() -> None:
    client = PolymarketUSClient(PolymarketSettings(product=PolymarketProduct.US))
    client._client = httpx.AsyncClient(transport=_429("30"))

    with pytest.raises(RateLimitError):
        await client.discover_markets(limit=1)

    assert client._limiter.paused is True
    await client.close()


async def test_a_429_without_retry_after_still_pauses() -> None:
    """Absent guidance is not permission to keep going at full rate."""
    client = KalshiClient(KalshiSettings())
    client._client = httpx.AsyncClient(transport=_429(None))

    with pytest.raises(RateLimitError) as caught:
        await client.discover_markets(limit=1)

    assert caught.value.retry_after is None
    assert client._limiter.paused is True
    await client.close()
