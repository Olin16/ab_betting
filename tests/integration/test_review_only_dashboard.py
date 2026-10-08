"""Reviewing a pair must not be a way to start trading.

Approval happens in the dashboard, and the example trial configuration enables the trial.
Put together, restarting `serve` to review a pair — against a database where a pair is
already approved — starts paper trading as a side effect of looking at it. An operator who
leaves the CLI trial running and opens the dashboard to watch gets a *second* runner
against the same database.

``EVENT_ARB_REVIEW_ONLY`` is the deliberate mode that cannot do either. It narrows what may
happen; it does not relax the checks, which is why the static scope and schema gate still
runs underneath it.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from event_arb.api.app import create_app
from event_arb.config import (
    OpportunitySettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
    TrialSettings,
    WatchedPair,
)
from event_arb.scanner import Scanner
from tests.conftest import KALSHI_FED, POLY_FED


def make_settings(fixture_dir: Path, **overrides: object) -> Settings:
    return Settings(
        polymarket=PolymarketSettings(product=PolymarketProduct.GLOBAL),
        fixture_dir=fixture_dir,
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
        trial=TrialSettings(
            enabled=True,
            watched_pairs=[WatchedPair(kalshi_market_id=KALSHI_FED, polymarket_market_id=POLY_FED)],
            scan_interval_seconds=0.05,
            refresh_interval_seconds=0.05,
        ),
        **overrides,  # type: ignore[arg-type]
    )


def serve(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, Scanner(settings))) as client:
        yield client


def state_of(client: TestClient) -> Any:
    """The app's state. ``TestClient.app`` is typed as a bare ASGI callable."""
    return client.app.state  # type: ignore[attr-defined]


def test_review_only_refuses_to_start_a_trial(fixture_dir: Path) -> None:
    """The trial is enabled in configuration and must still not start."""
    settings = make_settings(fixture_dir, review_only=True)
    assert settings.trial.enabled, "the point is that it is enabled and still does not run"

    for client in serve(settings):
        assert getattr(state_of(client), "trial", None) is None
        assert state_of(client).review_only is True
        assert client.get("/").status_code == 200


def test_the_same_configuration_without_review_only_does_start_one(fixture_dir: Path) -> None:
    """The control: without the flag, this configuration starts a runner.

    Observation-only so the run has something it is allowed to do — with no approved pair
    a *trading* run is refused by ``pair_readiness``, which is a different gate and would
    make this test pass for the wrong reason.
    """
    settings = make_settings(fixture_dir)
    settings.trial.observation_only = True
    for client in serve(settings):
        assert getattr(state_of(client), "trial", None) is not None


def test_review_only_skips_the_trial_gate_rather_than_failing_it(fixture_dir: Path) -> None:
    """The distinction that matters: the trial preflight is never even reached.

    Not starting because a gate refused and not starting because no trial was attempted
    are different states, and an operator reading the logs needs to see which one they
    are in.
    """
    settings = make_settings(fixture_dir)
    settings.trial.observation_only = True

    for client in serve(settings):
        assert getattr(state_of(client), "preflight", None) is not None, (
            "without the flag, the trial gate runs"
        )

    review = make_settings(fixture_dir, review_only=True)
    review.trial.observation_only = True
    for client in serve(review):
        assert getattr(state_of(client), "preflight", None) is None, (
            "with it, no trial is attempted, so no trial gate runs"
        )
        assert getattr(state_of(client), "static_preflight", None) is not None, (
            "but the static scope and schema checks still do"
        )


def test_review_only_says_so_on_every_page(fixture_dir: Path) -> None:
    settings = make_settings(fixture_dir, review_only=True)
    for client in serve(settings):
        for path in ("/", "/pairs", "/paper"):
            assert "Review only" in client.get(path).text


def test_review_only_still_allows_review_and_approval(fixture_dir: Path) -> None:
    """It narrows what may happen; it does not disable the thing it exists for."""
    settings = make_settings(fixture_dir, review_only=True)
    for client in serve(settings):
        scanner: Scanner = state_of(client).scanner
        pair_id = next(iter(scanner.state.pairs))
        review = client.get(f"/pairs/{pair_id}")
        assert review.status_code == 200
        assert "Approval" in review.text


def test_the_approval_command_shown_carries_a_bearer_token_when_one_is_required(
    fixture_dir: Path,
) -> None:
    """The command on the page has to be the command that works.

    With a token configured the unauthenticated curl this page used to print returns 401,
    so an operator's first attempt at the only human gate in the system failed for a
    reason the page never mentioned.
    """
    settings = make_settings(fixture_dir, review_only=True, api_auth_token="trial-token")
    for client in serve(settings):
        scanner: Scanner = state_of(client).scanner
        pair_id = next(iter(scanner.state.pairs))
        body = client.get(f"/pairs/{pair_id}").text
        assert "authorization: Bearer" in body
        assert "EVENT_ARB_API_AUTH_TOKEN" in body
        # Read from the environment, never pasted: a pasted token lands in shell history.
        assert "trial-token" not in body, "the token itself must never be rendered"
        assert "curl -fsS" in body, "without -f, curl exits zero on a refused approval"


def test_an_authenticated_approval_endpoint_actually_refuses_an_unauthenticated_call(
    fixture_dir: Path,
) -> None:
    """The instructions are only worth anything if the endpoint enforces them."""
    settings = make_settings(fixture_dir, review_only=True, api_auth_token="trial-token")
    for client in serve(settings):
        scanner: Scanner = state_of(client).scanner
        pair_id = next(iter(scanner.state.pairs))
        payload = {
            "approved_by": "operator@example.com",
            "reviewer_notes": "test",
            "kalshi_settlement_source": "",
            "polymarket_settlement_source": "",
        }
        unauthenticated = client.post(f"/api/pairs/{pair_id}/approve", json=payload)
        assert unauthenticated.status_code == 401

        authenticated = client.post(
            f"/api/pairs/{pair_id}/approve",
            json=payload,
            headers={"authorization": "Bearer trial-token"},
        )
        # Not necessarily 200 — this fixture pair may be unapprovable on its merits, which
        # is a different refusal entirely. What matters is that the token got past the guard.
        assert authenticated.status_code != 401


@pytest.mark.parametrize("path", ["/", "/pairs", "/opportunities"])
def test_a_normal_dashboard_shows_no_review_only_banner(fixture_dir: Path, path: str) -> None:
    settings = make_settings(fixture_dir)
    for client in serve(settings):
        assert "Review only" not in client.get(path).text
