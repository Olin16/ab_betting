"""The suite's network guard: fail-closed by default, read-only when opted in.

The first version of this guard patched the real httpx transport for *every* test,
including tests carrying the ``live`` marker with ``EVENT_ARB_RUN_LIVE_TESTS=1`` set. That
made the documented ``pytest -m live`` path impossible to run.

Nothing here reaches the network. The gate and the read-only check are exercised directly.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from tests.conftest import (
    LiveNetworkError,
    _live_access_allowed,
    _require_read_only,
)


class _Node:
    def __init__(self, live: bool) -> None:
        self._live = live

    def get_closest_marker(self, name: str) -> object | None:
        return object() if (name == "live" and self._live) else None


class _Request:
    def __init__(self, live: bool) -> None:
        self.node = _Node(live)


def _allowed(*, live_marker: bool, opted_in: bool, monkeypatch: pytest.MonkeyPatch) -> bool:
    if opted_in:
        monkeypatch.setenv("EVENT_ARB_RUN_LIVE_TESTS", "1")
    else:
        monkeypatch.delenv("EVENT_ARB_RUN_LIVE_TESTS", raising=False)
    return _live_access_allowed(_Request(live_marker))  # type: ignore[arg-type]


def test_an_ordinary_test_is_never_allowed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _allowed(live_marker=False, opted_in=False, monkeypatch=monkeypatch) is False


def test_the_marker_alone_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise `pytest -m live` fires on a machine that never asked for it."""
    assert _allowed(live_marker=True, opted_in=False, monkeypatch=monkeypatch) is False


def test_the_environment_variable_alone_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise opting in once opens the network to the whole suite."""
    assert _allowed(live_marker=False, opted_in=True, monkeypatch=monkeypatch) is False


def test_both_together_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _allowed(live_marker=True, opted_in=True, monkeypatch=monkeypatch) is True


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_only_methods_pass_the_live_check(method: str) -> None:
    _require_read_only(httpx.Request(method, "https://example.test/markets"))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writing_methods_are_refused_even_when_opted_in(method: str) -> None:
    """A live schema check that grows a POST must fail here, not at the venue."""
    with pytest.raises(LiveNetworkError, match="live tests are read-only"):
        _require_read_only(httpx.Request(method, "https://example.test/orders"))


async def test_an_ordinary_test_cannot_reach_a_venue_over_http() -> None:
    """The guard is autouse, so this test is already behind it."""
    async with httpx.AsyncClient(timeout=1.0) as client:
        with pytest.raises(AssertionError, match="never let the suite reach a live venue"):
            await client.get("https://external-api.demo.kalshi.co/trade-api/v2/markets")


def test_an_ordinary_test_cannot_open_a_raw_socket() -> None:
    """WebSocket clients bypass the httpx transport entirely."""
    with pytest.raises(AssertionError, match="raw socket connection"):
        socket.socket().connect(("example.test", 443))


def test_mock_transport_is_unaffected() -> None:
    """The guard must not push tests away from exercising the real clients."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get("https://example.test/anything").json() == {"ok": True}


def test_the_live_module_only_uses_read_only_verbs() -> None:
    """Structural check on the opted-in path, without running it."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "integration" / "test_live_readonly.py"
    tree = ast.parse(source.read_text())

    writing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"post", "put", "patch", "delete"}:
            writing.append(node.attr)
    assert writing == [], f"live tests must be read-only, found {writing}"


def test_the_live_module_is_deselected_by_default() -> None:
    """A live test that runs by accident is the failure this guard exists to prevent."""
    import tomllib
    from pathlib import Path

    config = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert "not live" in config["tool"]["pytest"]["ini_options"]["addopts"]
