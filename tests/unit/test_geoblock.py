"""Geoblock enforcement.

The single most important property here: the checker **fails closed**. Every ambiguous
outcome must resolve to "blocked". A check that failed open would convert a transient
network problem into a compliance violation.
"""

from __future__ import annotations

import httpx
import pytest

from event_arb.venues.polymarket.geoblock import GeoblockChecker


def _checker(handler: object) -> GeoblockChecker:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return GeoblockChecker(
        url="https://polymarket.com/api/geoblock",
        client=httpx.AsyncClient(transport=transport),
    )


async def test_allows_only_when_blocked_is_explicitly_false() -> None:
    checker = _checker(
        lambda request: httpx.Response(
            200, json={"blocked": False, "ip": "1.2.3.4", "country": "GB", "region": "ENG"}
        )
    )
    status = await checker.check()
    assert status.allowed is True
    assert status.country == "GB"


async def test_blocks_when_venue_says_blocked() -> None:
    checker = _checker(
        lambda request: httpx.Response(200, json={"blocked": True, "country": "US", "region": "NY"})
    )
    status = await checker.check()
    assert status.allowed is False
    assert status.blocked is True


async def test_network_error_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    status = await _checker(handler).check()
    assert status.allowed is False
    assert "failed" in status.reason


async def test_timeout_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    assert (await _checker(handler).check()).allowed is False


@pytest.mark.parametrize("status_code", [403, 429, 500, 503])
async def test_non_200_fails_closed(status_code: int) -> None:
    checker = _checker(lambda request: httpx.Response(status_code, json={"blocked": False}))
    assert (await checker.check()).allowed is False


async def test_non_json_fails_closed() -> None:
    checker = _checker(lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    assert (await checker.check()).allowed is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"blocked": "false"}, {"blocked": 0}, {"blocked": None}, {"country": "US"}],
)
async def test_missing_or_non_boolean_blocked_fails_closed(payload: dict[str, object]) -> None:
    """A string "false" is not a boolean false; coercing it would be a silent bypass."""
    checker = _checker(lambda request: httpx.Response(200, json=payload))
    assert (await checker.check()).allowed is False


async def test_blocked_result_is_never_served_from_cache_as_allowed() -> None:
    """Only allowed results are cached; a failure must trigger a fresh check next time."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    checker = _checker(handler)
    await checker.check()
    await checker.check()
    assert calls["n"] == 2


async def test_allowed_result_is_cached() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"blocked": False, "country": "GB", "region": "ENG"})

    checker = _checker(handler)
    await checker.check()
    await checker.check()
    assert calls["n"] == 1


async def test_force_bypasses_the_cache() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"blocked": False, "country": "GB", "region": "ENG"})

    checker = _checker(handler)
    await checker.check()
    await checker.check(force=True)
    assert calls["n"] == 2


async def test_ip_address_is_not_retained() -> None:
    """The endpoint returns the caller's IP; there is no reason to store it."""
    checker = _checker(
        lambda request: httpx.Response(
            200, json={"blocked": False, "ip": "203.0.113.7", "country": "GB", "region": "ENG"}
        )
    )
    status = await checker.check()
    assert "203.0.113.7" not in repr(status)
