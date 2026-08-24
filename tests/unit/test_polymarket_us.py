"""Polymarket US: mapper, fees, and the public read-only client.

Polymarket US is a different venue from Polymarket Global. These tests exist as much to
pin that separation as to check the parsing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from event_arb.config import PolymarketSettings
from event_arb.domain.enums import SettlementAsset, Venue
from event_arb.domain.models import FeeConfig
from event_arb.venues.base import MarketDataError, RateLimitError, TransientError
from event_arb.venues.polymarket_us.client import PolymarketUSClient
from event_arb.venues.polymarket_us.fees import (
    MAKER_REBATE_THETA,
    TAKER_THETA,
    maker_rebate,
    polymarket_us_fee_config,
    taker_fee,
    taker_rebate_fraction,
)
from event_arb.venues.polymarket_us.mapper import (
    PRODUCT,
    RulesCompleteness,
    parse_book,
    parse_market,
    parse_payload,
    rules_completeness,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "polymarket_us"


def load(name: str) -> object:
    return parse_payload((FIXTURES / name).read_text())


# ----------------------------------------------------------------------- fees


def test_fee_formula_matches_the_published_schedule() -> None:
    """Fee = theta * C * p * (1 - p), theta 0.06 for takers."""
    config = polymarket_us_fee_config()
    assert config.taker_rate == TAKER_THETA
    # 0.06 * 100 * 0.5 * 0.5 = 1.50
    assert taker_fee(Decimal("100"), Decimal("0.50"), config) == Decimal("1.50")


def test_fee_decays_toward_the_extremes() -> None:
    config = polymarket_us_fee_config()
    mid = taker_fee(Decimal("100"), Decimal("0.50"), config)
    edge = taker_fee(Decimal("100"), Decimal("0.05"), config)
    assert edge < mid


@pytest.mark.parametrize(
    ("quantity", "price", "expected"),
    [
        # 0.06*1*0.5*0.5 = 0.015 -> banker's rounding to nearest cent lands on 0.02
        # (round half to even: 1 is odd, 2 is even).
        ("1", "0.50", "0.02"),
        # 0.06*3*0.5*0.5 = 0.045 -> half-way, rounds to the even cent 0.04, NOT 0.05.
        ("3", "0.50", "0.04"),
        # 0.06*5*0.5*0.5 = 0.075 -> rounds to 0.08 (8 is even).
        ("5", "0.50", "0.08"),
    ],
)
def test_bankers_rounding_at_the_half_cent_boundary(
    quantity: str, price: str, expected: str
) -> None:
    """Round-half-to-even, not ceiling.

    This is the sharpest difference from the other venues: Kalshi ceils to a centicent and
    Polymarket Global ceils to five decimals, so a copied implementation would overstate
    Polymarket US fees at exactly these boundaries.
    """
    config = polymarket_us_fee_config()
    assert taker_fee(Decimal(quantity), Decimal(price), config) == Decimal(expected)


def test_rounding_is_not_a_ceiling() -> None:
    """0.045 must round down to 0.04; a ceiling model would say 0.05."""
    config = polymarket_us_fee_config()
    assert taker_fee(Decimal("3"), Decimal("0.50"), config) == Decimal("0.04")


def test_maker_rebate_is_held_positive_not_as_a_negative_rate() -> None:
    """The schedule publishes -0.0125; a negative 'rate' invites a dropped sign."""
    config = polymarket_us_fee_config()
    assert config.maker_rate == Decimal("0")
    assert config.maker_rebate_rate == MAKER_REBATE_THETA
    assert maker_rebate(Decimal("100"), Decimal("0.50"), config) > 0


def test_zero_quantity_is_free() -> None:
    assert taker_fee(Decimal("0"), Decimal("0.5"), polymarket_us_fee_config()) == Decimal("0")


@pytest.mark.parametrize(
    ("volume", "fraction"),
    [("0", "0"), ("249999", "0"), ("250000", "0.10"), ("1000000", "0.25"), ("10000000", "0.50")],
)
def test_taker_rebate_tiers(volume: str, fraction: str) -> None:
    assert taker_rebate_fraction(Decimal(volume)) == Decimal(fraction)


def test_taker_rebate_is_never_netted_into_a_quoted_fee() -> None:
    """Rebates are retrospective and volume-tiered; assuming one understates cost."""
    config = polymarket_us_fee_config()
    fee = taker_fee(Decimal("100"), Decimal("0.50"), config)
    assert fee == Decimal("1.50")  # full fee, no rebate applied


def test_fee_parameters_are_configuration_not_constants() -> None:
    custom = polymarket_us_fee_config(taker_theta=Decimal("0.03"), source="override")
    assert taker_fee(Decimal("100"), Decimal("0.50"), custom) == Decimal("0.75")


# --------------------------------------------------------------------- mapper


def test_market_settles_in_usd_not_usdc() -> None:
    """No haircut, no depeg, no Polygon: this is the whole point of the US product."""
    payload = load("markets.json")
    assert isinstance(payload, dict)
    market = parse_market(payload["data"][0])
    assert market.settlement_asset is SettlementAsset.USD
    assert market.venue_market_id == "fed-cuts-25bps-september-2026"


def test_market_records_the_product_discriminator() -> None:
    payload = load("markets.json")
    assert isinstance(payload, dict)
    market = parse_market(payload["data"][0])
    assert market.raw_metadata["_product"] == PRODUCT


def test_tick_size_and_minimum_quantity_are_carried_through() -> None:
    payload = load("markets.json")
    assert isinstance(payload, dict)
    market = parse_market(payload["data"][0])
    assert market.tick_size == Decimal("0.01")
    assert market.min_quantity_increment == Decimal("1")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", RulesCompleteness.MISSING),
        ("A market about something.", RulesCompleteness.INSUFFICIENT),
        ("short but says resolve and settle", RulesCompleteness.INSUFFICIENT),
    ],
)
def test_rules_completeness_classification(text: str, expected: RulesCompleteness) -> None:
    assert rules_completeness(text) is expected


def test_full_rules_text_is_recognised_as_likely_complete() -> None:
    payload = load("markets.json")
    assert isinstance(payload, dict)
    assert rules_completeness(payload["data"][0]["description"]) is (
        RulesCompleteness.LIKELY_COMPLETE
    )


def test_market_without_usable_rules_is_not_tradable() -> None:
    """An approval is a claim two contracts settle alike; it cannot rest on a blurb.

    The documentation does not state that `description` carries the complete enforceable
    rules, so a market whose text looks insufficient is withheld from approval rather than
    approved against partial information.
    """
    payload = load("markets.json")
    assert isinstance(payload, dict)
    thin = parse_market(payload["data"][1])
    missing = parse_market(payload["data"][2])
    assert thin.is_tradable is False
    assert missing.is_tradable is False
    assert thin.raw_metadata["_rules_completeness"] == "insufficient"
    assert missing.raw_metadata["_rules_completeness"] == "missing"


def test_complete_market_is_tradable() -> None:
    payload = load("markets.json")
    assert isinstance(payload, dict)
    assert parse_market(payload["data"][0]).is_tradable is True


def test_book_parses_bids_and_offers_with_px_qty() -> None:
    payload = load("books/fed-cuts-25bps-september-2026.json")
    assert isinstance(payload, dict)
    book = parse_book("slug", payload)
    assert book.yes.bids[0].price == Decimal("0.62")
    assert book.yes.asks[0].price == Decimal("0.64")
    assert book.yes.asks[0].quantity == Decimal("250")
    assert book.venue is Venue.POLYMARKET


def test_book_depth_is_ordered_best_first() -> None:
    payload = load("books/fed-cuts-25bps-september-2026.json")
    assert isinstance(payload, dict)
    book = parse_book("slug", payload)
    assert [level.price for level in book.yes.bids] == [
        Decimal("0.62"),
        Decimal("0.61"),
        Decimal("0.60"),
    ]
    assert [level.price for level in book.yes.asks] == [
        Decimal("0.64"),
        Decimal("0.65"),
        Decimal("0.67"),
    ]


def test_no_side_is_derived_by_reciprocity_when_absent() -> None:
    payload = load("books/fed-cuts-25bps-september-2026.json")
    assert isinstance(payload, dict)
    book = parse_book("slug", payload)
    # YES ask 0.64 -> NO bid 0.36; YES bid 0.62 -> NO ask 0.38.
    assert book.yes.asks[0].price + book.no.bids[0].price == Decimal("1")
    assert book.yes.bids[0].price + book.no.asks[0].price == Decimal("1")


def test_book_carries_no_sequence_number() -> None:
    """Continuity is unproven on the public book, so nothing may assume it."""
    payload = load("books/fed-cuts-25bps-september-2026.json")
    assert isinstance(payload, dict)
    assert parse_book("slug", payload).sequence is None


def test_prices_are_exact_decimals_never_floats() -> None:
    payload = load("books/fed-cuts-25bps-september-2026.json")
    assert isinstance(payload, dict)
    book = parse_book("slug", payload)
    assert all(isinstance(level.price, Decimal) for level in book.yes.bids)


def test_float_in_payload_is_refused() -> None:
    """Guards a caller that used response.json() instead of parse_payload()."""
    with pytest.raises(MarketDataError, match="parse_float=Decimal"):
        parse_book("slug", {"bids": [{"px": 0.62, "qty": 100}], "offers": []})


@pytest.mark.parametrize(
    "payload",
    [
        {"bids": "not-a-list"},
        {"bids": ["not-an-object"]},
        {"bids": [{"qty": "100"}]},
    ],
)
def test_malformed_book_raises(payload: dict[str, object]) -> None:
    with pytest.raises(MarketDataError):
        parse_book("slug", payload)


def test_market_without_identity_raises() -> None:
    with pytest.raises(MarketDataError):
        parse_market({"question": "no slug or id"})


# --------------------------------------------------------------------- client


def settings() -> PolymarketSettings:
    return PolymarketSettings()


def transport(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_client_sends_no_credentials() -> None:
    """The public gateway needs no key, and this client holds none."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = PolymarketUSClient(settings(), client=transport(handler))
    await client.discover_markets()

    assert seen
    headers = {k.lower() for k in seen[0].headers}
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert "key" not in seen[0].url.params


async def test_client_targets_the_public_gateway() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    await PolymarketUSClient(settings(), client=transport(handler)).discover_markets()
    assert str(seen[0].url).startswith("https://gateway.polymarket.us/v1/markets")


async def test_targeted_retrieval_fetches_only_named_markets() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": {"slug": "s1", "question": "Q", "active": True}})

    await PolymarketUSClient(settings(), client=transport(handler)).discover_markets(
        market_ids=["s1", "s2"]
    )
    assert seen == ["/v1/market/slug/s1", "/v1/market/slug/s2"]


async def test_full_book_is_fetched_for_a_market() -> None:
    body = json.loads((FIXTURES / "books/fed-cuts-25bps-september-2026.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/markets/slug-1/book"
        return httpx.Response(200, json=body)

    book = await PolymarketUSClient(settings(), client=transport(handler)).fetch_orderbook("slug-1")
    assert len(book.yes.asks) == 3


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, RateLimitError), (500, TransientError), (503, TransientError), (404, MarketDataError)],
)
async def test_http_errors_map_to_the_normalized_taxonomy(
    status: int, error: type[Exception]
) -> None:
    client = PolymarketUSClient(
        settings(), client=transport(lambda request: httpx.Response(status))
    )
    with pytest.raises(error):
        await client.fetch_orderbook("slug-1")


async def test_streaming_refuses_rather_than_returning_an_empty_feed() -> None:
    """Silently yielding nothing would let a caller believe it has live ingestion."""
    client = PolymarketUSClient(settings(), client=transport(lambda r: httpx.Response(200)))
    with pytest.raises(NotImplementedError, match="authenticated API"):
        client.stream(["slug-1"])


def test_client_has_no_order_entry_surface() -> None:
    forbidden = {"submit", "submit_order", "place_order", "create_order", "buy", "sell"}
    assert not (forbidden & set(dir(PolymarketUSClient)))


def test_us_adapter_uses_no_global_concepts_in_code() -> None:
    """No wallets, token ids, USDC, Polygon, or geoblock logic may leak across.

    Checked against identifiers and imports rather than raw text, because the docstrings
    legitimately *name* these concepts in order to say they do not apply.
    """
    import ast

    source_dir = Path(__file__).parent.parent.parent / "src/event_arb/venues/polymarket_us"
    forbidden = ("token_id", "condition_id", "polygon", "wallet", "geoblock", "usdc")

    identifiers: set[str] = set()
    imports: set[str] = set()
    for path in source_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr.lower())
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg.lower())
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                identifiers.add(node.name.lower())
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.lower())
            elif isinstance(node, ast.Import):
                imports.update(alias.name.lower() for alias in node.names)

    for concept in forbidden:
        leaked = [name for name in identifiers if concept in name]
        assert not leaked, f"Global concept '{concept}' used in US adapter code: {leaked}"

    assert not [m for m in imports if m.startswith("event_arb.venues.polymarket.")], imports


def test_us_fee_config_differs_from_global() -> None:
    from event_arb.venues.polymarket.fees import polymarket_fee_config

    us = polymarket_us_fee_config()
    glob = polymarket_fee_config("politics")
    assert us.taker_rate != glob.taker_rate
    assert isinstance(us, FeeConfig)
