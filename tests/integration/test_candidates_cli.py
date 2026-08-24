"""CLI integration for `candidates discover`.

The confirmation gate is only worth something if the plan it shows is the plan that runs,
so these assert plan-vs-reality rather than plan-vs-expectation. **No live calls.**
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.candidates import CredentialsWouldBeSentError, DiscoveryLimits, build_request_plan
from event_arb.config import (
    DataSource,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import Venue, VenueProduct
from event_arb.main import build_parser, main
from event_arb.scanner import build_adapters


def settings_for(fixture_dir: Path, product: PolymarketProduct) -> Settings:
    return Settings(
        fixture_dir=fixture_dir,
        database_url="sqlite+aiosqlite:///:memory:",
        polymarket=PolymarketSettings(product=product),
    )


def plan_for(settings: Settings) -> object:
    adapters = build_adapters(settings)
    product = (
        VenueProduct.POLYMARKET_US
        if settings.polymarket.product is PolymarketProduct.US
        else VenueProduct.POLYMARKET_GLOBAL
    )
    return build_request_plan(
        DiscoveryLimits(), adapters[Venue.KALSHI], adapters[Venue.POLYMARKET], product
    )


# ------------------------------------------------------------------ parser wiring


def test_parser_exposes_the_documented_flags() -> None:
    args = build_parser().parse_args(
        [
            "candidates",
            "discover",
            "--kalshi-limit",
            "250",
            "--polymarket-limit",
            "250",
            "--min-score",
            "0.55",
            "--max-per-market",
            "3",
            "--max-results",
            "100",
            "--recheck-top",
            "25",
        ]
    )
    assert args.kalshi_limit == 250
    assert args.min_score == "0.55"
    assert args.recheck_top == 25
    assert args.authenticated_mode is False
    assert args.yes_i_authorize_live_requests is False


def test_authenticated_mode_is_a_separate_named_flag() -> None:
    args = build_parser().parse_args(["candidates", "discover", "--authenticated-mode"])
    assert args.authenticated_mode is True


# ------------------------------------------------- plan matches what will be sent


def test_plan_reflects_the_selected_product(fixture_dir: Path) -> None:
    """Global selection must never display a US endpoint."""
    us = plan_for(settings_for(fixture_dir, PolymarketProduct.US)).render()  # type: ignore[attr-defined]
    glob = plan_for(settings_for(fixture_dir, PolymarketProduct.GLOBAL)).render()  # type: ignore[attr-defined]

    assert "polymarket_us" in us
    assert "polymarket_us" not in glob
    assert "/v1/markets" in us
    assert "/v1/markets" not in glob


def test_plan_urls_are_the_adapters_own_endpoints(fixture_dir: Path) -> None:
    settings = settings_for(fixture_dir, PolymarketProduct.US)
    adapters = build_adapters(settings)
    plan = plan_for(settings)
    rendered = plan.render()  # type: ignore[attr-defined]
    assert adapters[Venue.KALSHI].discovery_base_url in rendered  # type: ignore[attr-defined]
    assert adapters[Venue.POLYMARKET].discovery_base_url in rendered  # type: ignore[attr-defined]


def test_fixture_mode_plan_shows_no_live_host(fixture_dir: Path) -> None:
    """A fixture run must not display an endpoint it will never contact."""
    rendered = plan_for(settings_for(fixture_dir, PolymarketProduct.US)).render()  # type: ignore[attr-defined]
    assert "fixture corpus" in rendered
    assert "https://" not in rendered


def test_plan_declares_credentials_truthfully(fixture_dir: Path) -> None:
    plan = plan_for(settings_for(fixture_dir, PolymarketProduct.US))
    assert plan.sends_any_credentials is False  # type: ignore[attr-defined]
    assert "credentials         : none" in plan.render()  # type: ignore[attr-defined]


def test_plan_declares_no_books_and_no_websockets(fixture_dir: Path) -> None:
    rendered = plan_for(settings_for(fixture_dir, PolymarketProduct.US)).render()  # type: ignore[attr-defined]
    assert "order books fetched : 0" in rendered
    assert "websockets opened   : 0" in rendered


def test_plan_reports_pagination_is_not_followed(fixture_dir: Path) -> None:
    rendered = plan_for(settings_for(fixture_dir, PolymarketProduct.US)).render()  # type: ignore[attr-defined]
    assert "NOT followed" in rendered


# --------------------------------------------------------- credential refusal


def test_a_credentialled_kalshi_client_is_reported_as_sending_credentials(
    tmp_path: Path,
) -> None:
    """The planner reads the adapter, so a signer makes the plan say so."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    from event_arb.config import KalshiSettings
    from event_arb.venues.kalshi.auth import KalshiSigner
    from event_arb.venues.kalshi.client import KalshiClient

    signer = KalshiSigner(
        api_key_id="k",
        private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )
    client = KalshiClient(KalshiSettings(), signer=signer)
    assert client.sends_credentials is True

    plan = build_request_plan(DiscoveryLimits(), client, client, VenueProduct.POLYMARKET_US)
    assert plan.sends_any_credentials is True
    assert "SENDS CREDENTIALS" in plan.render()


def test_credential_refusal_error_names_the_escape_hatch() -> None:
    error = CredentialsWouldBeSentError(
        "credentials are configured, so this run would transmit them, but the plan above "
        "is the credential-free one. Re-run with --authenticated-mode to authorize that "
        "explicitly, or unset the venue credentials."
    )
    assert "--authenticated-mode" in str(error)


# ------------------------------------------------------------- end-to-end run


def test_fixture_run_makes_no_network_call(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest guarantee available: httpx cannot be used at all."""
    import httpx

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a fixture-mode run attempted a network request")

    monkeypatch.setattr(httpx.AsyncClient, "request", explode)
    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    monkeypatch.setenv("EVENT_ARB_FIXTURE_DIR", str(fixture_dir))
    monkeypatch.setenv("EVENT_ARB_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    exit_code = main(["candidates", "discover", "--max-results", "5", "--recheck-top", "2"])
    assert exit_code == 0


def test_fixture_run_never_selects_a_live_adapter(fixture_dir: Path) -> None:
    from event_arb.venues.fixture import FixtureAdapter

    adapters = build_adapters(settings_for(fixture_dir, PolymarketProduct.US))
    assert all(isinstance(a, FixtureAdapter) for a in adapters.values())


def test_live_mode_for_us_requires_rest_polling(fixture_dir: Path) -> None:
    from event_arb.config import IngestionMode

    settings = Settings(
        fixture_dir=fixture_dir,
        data_source=DataSource.LIVE,
        ingestion_mode=IngestionMode.WEBSOCKET,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    with pytest.raises(ValueError, match="authenticated API"):
        settings.validate_live_market_data()


def test_min_score_is_parsed_as_decimal_not_float() -> None:
    """0.55 through a float would not be 0.55."""
    args = build_parser().parse_args(["candidates", "discover", "--min-score", "0.55"])
    assert Decimal(str(args.min_score)) == Decimal("0.55")
