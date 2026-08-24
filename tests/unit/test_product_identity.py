"""Product identity: Polymarket Global and US must never be interchangeable."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from event_arb.domain.enums import (
    FeePolicy,
    MatchStatus,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.models import (
    SETTLEMENT_FINGERPRINT_VERSION,
    FeeConfig,
    MarketPair,
    VenueMarket,
)
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
        id="p1",
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

    markets = {
        (Venue.KALSHI, "K"): kalshi_market(),
        (Venue.POLYMARKET, "SAME-ID"): us_market,
    }
    observed = set(markets)
    pairs, report = restore_pairs({"p1": pair}, markets, observed=observed)

    assert pairs["p1"].is_tradable_pair is False
    assert pairs["p1"].status is MatchStatus.SUSPENDED
    assert "can never satisfy a US pair" in report.suspended[0][1]


def test_a_matching_product_restores_normally() -> None:
    us_market = market(VenueProduct.POLYMARKET_US)
    pair = approved_pair(
        VenueProduct.POLYMARKET_US, us_market.fingerprint_with_source("fomc statement")
    )
    markets = {
        (Venue.KALSHI, "K"): kalshi_market(),
        (Venue.POLYMARKET, "SAME-ID"): us_market,
    }
    pairs, report = restore_pairs({"p1": pair}, markets, observed=set(markets))
    assert pairs["p1"].is_tradable_pair is True
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
