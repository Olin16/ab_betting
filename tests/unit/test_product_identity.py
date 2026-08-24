"""Product identity: Polymarket Global and US must never be interchangeable."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.domain.enums import (
    FeePolicy,
    MatchStatus,
    Outcome,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.keys import kalshi_key, market_key, polymarket_key
from event_arb.domain.models import (
    SETTLEMENT_FINGERPRINT_VERSION,
    BookLevel,
    FeeConfig,
    MarketPair,
    OrderBook,
    OrderBookSide,
    VenueMarket,
)
from event_arb.matching.candidates import pair_id_for
from event_arb.orderbooks.book import BookStore
from event_arb.persistence.restore import restore_pairs

FEE = FeeConfig(
    venue=Venue.POLYMARKET,
    taker_rate=Decimal("0.06"),
    fee_policy=FeePolicy.POLYMARKET_US_BANKERS_CENT,
)


def market(product: VenueProduct, **overrides: object) -> VenueMarket:
    base: dict[str, object] = {
        "venue": Venue.POLYMARKET,
        "venue_market_id": "SAME-ID",
        "title": "Fed cuts 25bps?",
        "rules_text": "Resolves Yes if the FOMC cuts 25bps.",
        "settlement_source": "FOMC statement",
        "close_time": datetime(2026, 9, 17, 18, tzinfo=UTC),
        "fee_config": FEE,
        "product": product,
    }
    return VenueMarket(**{**base, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------- fingerprint


def test_product_is_part_of_the_settlement_fingerprint() -> None:
    """Otherwise two products sharing an id and rules would look like one contract."""
    a = market(VenueProduct.POLYMARKET_GLOBAL, settlement_asset=SettlementAsset.USD)
    b = market(VenueProduct.POLYMARKET_US, settlement_asset=SettlementAsset.USD)
    assert a.settlement_fingerprint != b.settlement_fingerprint


def test_settlement_deadline_is_part_of_the_fingerprint() -> None:
    """A different deadline is a different capital lockup."""
    base = market(VenueProduct.POLYMARKET_US)
    moved = base.model_copy(update={"settlement_deadline": datetime(2026, 10, 1, tzinfo=UTC)})
    assert base.settlement_fingerprint != moved.settlement_fingerprint


def test_fingerprint_version_bumped_to_invalidate_older_approvals() -> None:
    """Approvals granted under a weaker fingerprint must be re-reviewed.

    v4 added the reviewed settlement authority, so approvals made when the authority was
    unrecorded no longer validate.
    """
    assert SETTLEMENT_FINGERPRINT_VERSION == "v4"


# ------------------------------------------------------------------------ restore


def kalshi_market() -> VenueMarket:
    return VenueMarket(
        venue=Venue.KALSHI,
        venue_market_id="K",
        title="t",
        rules_text="r",
        fee_config=FeeConfig(
            venue=Venue.KALSHI,
            taker_rate=Decimal("0.07"),
            fee_policy=FeePolicy.KALSHI_CENTICENT_CEIL,
        ),
    )


def approved_pair(product: VenueProduct, fingerprint: str) -> MarketPair:
    """``fingerprint`` must be a source-aware fingerprint, as approval stores."""
    return MarketPair(
        id=pair_id_for("K", "SAME-ID", product),
        kalshi_market_id="K",
        polymarket_market_id="SAME-ID",
        polymarket_product=product,
        status=MatchStatus.APPROVED,
        manually_approved=True,
        kalshi_rules_hash_at_approval=kalshi_market().fingerprint_with_source(""),
        polymarket_rules_hash_at_approval=fingerprint,
    )


def test_a_global_approval_cannot_satisfy_a_us_pair() -> None:
    """The central guarantee of product identity."""
    global_market = market(VenueProduct.POLYMARKET_GLOBAL)
    us_market = market(VenueProduct.POLYMARKET_US)
    pair = approved_pair(VenueProduct.POLYMARKET_GLOBAL, global_market.settlement_fingerprint)

    # Deliberately mis-keyed: a US market filed under the Global key. Product-aware keys
    # make this unreachable in normal operation, so this exercises restore's own product
    # check as defence in depth against a future keying bug.
    markets = {
        kalshi_key("K"): kalshi_market(),
        polymarket_key(VenueProduct.POLYMARKET_GLOBAL, "SAME-ID"): us_market,
    }
    observed = set(markets)
    pairs, report = restore_pairs({pair.id: pair}, markets, observed=observed)

    assert pairs[pair.id].is_tradable_pair is False
    assert pairs[pair.id].status is MatchStatus.SUSPENDED
    assert "can never satisfy a US pair" in report.suspended[0][1]
    assert global_market.product is VenueProduct.POLYMARKET_GLOBAL


def test_correct_keying_makes_the_wrong_product_simply_absent() -> None:
    """With product in the key, a US market cannot even be found by a Global lookup."""
    us_market = market(VenueProduct.POLYMARKET_US)
    pair = approved_pair(VenueProduct.POLYMARKET_GLOBAL, us_market.settlement_fingerprint)

    markets = {kalshi_key("K"): kalshi_market(), market_key(us_market): us_market}
    pairs, report = restore_pairs({pair.id: pair}, markets, observed=set(markets))

    assert pairs[pair.id].is_tradable_pair is False
    assert "metadata unavailable" in report.suspended[0][1]


def test_a_matching_product_restores_normally() -> None:
    us_market = market(VenueProduct.POLYMARKET_US)
    pair = approved_pair(
        VenueProduct.POLYMARKET_US, us_market.fingerprint_with_source("fomc statement")
    )
    markets = {kalshi_key("K"): kalshi_market(), market_key(us_market): us_market}
    pairs, report = restore_pairs({pair.id: pair}, markets, observed=set(markets))
    assert pairs[pair.id].is_tradable_pair is True
    assert report.approvals_kept == 1


def test_generated_pairs_default_conservatively_to_global() -> None:
    """A pair that never stated a product is legacy data, so it is Global."""
    assert (
        MarketPair(id="x", kalshi_market_id="K", polymarket_market_id="P").polymarket_product
        is VenueProduct.POLYMARKET_GLOBAL
    )


# ------------------------------------------------------------------- fee dispatch


def test_kalshi_has_no_product_to_discriminate() -> None:
    assert kalshi_market().product is VenueProduct.NONE


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        (VenueProduct.POLYMARKET_GLOBAL, True),
        (VenueProduct.POLYMARKET_US, True),
        (VenueProduct.NONE, False),
    ],
)
def test_is_polymarket_helper(product: VenueProduct, expected: bool) -> None:
    assert product.is_polymarket is expected


def _book(product: VenueProduct, ask: str) -> OrderBook:
    return OrderBook(
        venue=Venue.POLYMARKET,
        product=product,
        market_id="SAME-ID",
        yes=OrderBookSide(
            outcome=Outcome.YES, asks=(BookLevel(price=Decimal(ask), quantity=Decimal("100")),)
        ),
        no=OrderBookSide(outcome=Outcome.NO),
    )


def test_the_book_store_keeps_the_two_products_apart() -> None:
    """Same market id on both products used to overwrite one book with the other."""
    store = BookStore()
    store.put(_book(VenueProduct.POLYMARKET_GLOBAL, "0.60"))
    store.put(_book(VenueProduct.POLYMARKET_US, "0.90"))

    assert len(store) == 2
    glob = store.get(polymarket_key(VenueProduct.POLYMARKET_GLOBAL, "SAME-ID"))
    us = store.get(polymarket_key(VenueProduct.POLYMARKET_US, "SAME-ID"))
    assert glob is not None and glob.yes.asks[0].price == Decimal("0.60")
    assert us is not None and us.yes.asks[0].price == Decimal("0.90")


def test_marking_one_product_stale_leaves_the_other_alone() -> None:
    store = BookStore()
    store.put(_book(VenueProduct.POLYMARKET_GLOBAL, "0.60"))
    store.put(_book(VenueProduct.POLYMARKET_US, "0.90"))

    store.mark_stale(polymarket_key(VenueProduct.POLYMARKET_US, "SAME-ID"))

    glob = store.get(polymarket_key(VenueProduct.POLYMARKET_GLOBAL, "SAME-ID"))
    us = store.get(polymarket_key(VenueProduct.POLYMARKET_US, "SAME-ID"))
    assert us is not None and us.is_stale is True
    assert glob is not None and glob.is_stale is False


def test_a_snapshot_records_the_product_its_book_came_from() -> None:
    """Product used to be a caller-supplied argument defaulting to 'none'."""
    assert _book(VenueProduct.POLYMARKET_US, "0.90").key == (
        Venue.POLYMARKET,
        VenueProduct.POLYMARKET_US,
        "SAME-ID",
    )


def test_a_pair_id_from_the_old_scheme_loses_its_approval() -> None:
    """Pair ids gained a product component; ids minted before that are ambiguous."""
    us = market(VenueProduct.POLYMARKET_US)
    pair = approved_pair(VenueProduct.POLYMARKET_US, us.fingerprint_with_source("fomc statement"))
    legacy = pair.model_copy(update={"id": "0123456789abcdef0123456789abcdef"})

    markets = {kalshi_key("K"): kalshi_market(), market_key(us): us}
    pairs, report = restore_pairs({legacy.id: legacy}, markets, observed=set(markets))

    assert pairs[legacy.id].is_tradable_pair is False
    assert "predates the current identity scheme" in report.suspended[0][1]
