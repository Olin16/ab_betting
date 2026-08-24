"""Scan-cycle behaviour: staleness suppression, auto-suspension, dedup, cooldown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from event_arb.config import OpportunitySettings, PolymarketSettings
from event_arb.domain.enums import (
    MatchStatus,
    OpportunityLabel,
    Outcome,
    RejectionReason,
    RiskFlag,
    SettlementAsset,
    Venue,
    VenueProduct,
)
from event_arb.domain.keys import MarketKey, kalshi_key, polymarket_key
from event_arb.domain.models import (
    BookLevel,
    MarketPair,
    OrderBook,
    OrderBookSide,
    VenueMarket,
)
from event_arb.opportunities.service import OpportunityService
from event_arb.orderbooks.book import BookStore
from event_arb.orderbooks.freshness import FreshnessPolicy
from event_arb.venues.kalshi.fees import default_kalshi_fee_config
from event_arb.venues.polymarket.fees import polymarket_fee_config

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
KALSHI_KEY = kalshi_key("K1")
POLY_KEY = polymarket_key(VenueProduct.POLYMARKET_GLOBAL, "P1")
CLOSE = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)

SETTINGS = OpportunitySettings(
    min_net_profit_usd=Decimal("0.10"),
    min_return_pct=Decimal("0.1"),
    slippage_buffer_pct=Decimal("0"),
    operational_cost_usd=Decimal("0"),
    max_quantity=Decimal("100"),
    max_book_age_ms=2000,
    alert_cooldown_seconds=300,
)


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in pairs)


def kalshi_book(received_at: datetime, sequence: int | None = 5) -> OrderBook:
    return OrderBook(
        venue=Venue.KALSHI,
        product=VenueProduct.NONE,
        market_id="K1",
        yes=OrderBookSide(
            outcome=Outcome.YES, bids=levels(("0.53", "100")), asks=levels(("0.55", "300"))
        ),
        no=OrderBookSide(
            outcome=Outcome.NO, bids=levels(("0.45", "300")), asks=levels(("0.47", "100"))
        ),
        received_at=received_at,
        sequence=sequence,
    )


def poly_book(received_at: datetime) -> OrderBook:
    return OrderBook(
        venue=Venue.POLYMARKET,
        product=VenueProduct.POLYMARKET_GLOBAL,
        market_id="P1",
        yes=OrderBookSide(
            outcome=Outcome.YES, bids=levels(("0.62", "200")), asks=levels(("0.64", "250"))
        ),
        no=OrderBookSide(
            outcome=Outcome.NO, bids=levels(("0.36", "250")), asks=levels(("0.38", "300"))
        ),
        received_at=received_at,
        sequence=None,
    )


def markets(rules: str = "shared rules") -> dict[MarketKey, VenueMarket]:
    return {
        KALSHI_KEY: VenueMarket(
            venue=Venue.KALSHI,
            venue_market_id="K1",
            title="K",
            rules_text=rules,
            close_time=CLOSE,
            fee_config=default_kalshi_fee_config(),
            settlement_asset=SettlementAsset.USD,
        ),
        POLY_KEY: VenueMarket(
            venue=Venue.POLYMARKET,
            venue_market_id="P1",
            title="P",
            rules_text=rules,
            close_time=CLOSE,
            fee_config=polymarket_fee_config("geopolitical"),
            settlement_asset=SettlementAsset.USDC,
        ),
    }


def approved_pair(rules: str = "shared rules") -> MarketPair:
    market_map = markets(rules)
    return MarketPair(
        id="pair1",
        kalshi_market_id="K1",
        polymarket_market_id="P1",
        status=MatchStatus.APPROVED,
        manually_approved=True,
        # Approval stores a source-aware fingerprint, so the test must build the same one.
        kalshi_rules_hash_at_approval=market_map[KALSHI_KEY].fingerprint_with_source(
            market_map[KALSHI_KEY].settlement_source.lower()
        ),
        polymarket_rules_hash_at_approval=market_map[POLY_KEY].fingerprint_with_source(
            market_map[POLY_KEY].settlement_source.lower()
        ),
    )


def build_service(book_age_ms: int = 0) -> tuple[OpportunityService, BookStore]:
    store = BookStore(FreshnessPolicy(max_age_ms=SETTINGS.max_book_age_ms))
    received = NOW - timedelta(milliseconds=book_age_ms)
    store.put(kalshi_book(received))
    store.put(poly_book(received))
    service = OpportunityService(store, SETTINGS, PolymarketSettings(usdc_haircut_pct=Decimal("0")))
    return service, store


def test_approved_pair_produces_a_confirmed_opportunity() -> None:
    service, _ = build_service()
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert len(result.confirmed) == 1
    opportunity = result.confirmed[0]
    assert opportunity.label is OpportunityLabel.CONFIRMED
    assert opportunity.net_profit > Decimal("0")
    assert opportunity.executable_quantity == Decimal("100")


def test_every_opportunity_is_flagged_non_atomic() -> None:
    """No opportunity this system emits is risk-free, and the record must say so."""
    service, _ = build_service()
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert all(RiskFlag.NON_ATOMIC_EXECUTION in o.risk_flags for o in result.opportunities)


def test_polymarket_leg_always_flags_missing_sequence_numbers() -> None:
    service, _ = build_service()
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert RiskFlag.NO_SEQUENCE_NUMBERS in result.opportunities[0].risk_flags


def test_usd_usdc_basis_is_always_flagged() -> None:
    service, _ = build_service()
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert RiskFlag.USD_USDC_BASIS in result.opportunities[0].risk_flags


def test_stale_book_suppresses_all_opportunities() -> None:
    """A book older than the limit cannot support an executable claim."""
    service, _ = build_service(book_age_ms=5000)
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert result.opportunities == []
    assert {r.reason for r in result.rejections} == {RejectionReason.BOOK_STALE}


def test_explicitly_stale_book_suppresses_opportunities() -> None:
    """A sequence gap marks the book stale even when it is only milliseconds old."""
    service, store = build_service()
    store.mark_stale(KALSHI_KEY)
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert result.opportunities == []
    assert all(r.reason is RejectionReason.BOOK_STALE for r in result.rejections)


def test_missing_book_is_rejected_not_ignored() -> None:
    store = BookStore(FreshnessPolicy(max_age_ms=2000))
    store.put(kalshi_book(NOW))
    service = OpportunityService(store, SETTINGS)
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert result.opportunities == []
    assert result.rejections[0].reason is RejectionReason.BOOK_MISSING


def test_rules_change_suspends_the_pair_and_blocks_detection() -> None:
    """The end-to-end safety behaviour: a rules edit stops confirmation immediately."""
    service, _ = build_service()
    pair = approved_pair(rules="original rules")
    edited = markets(rules="rewritten rules with a different threshold")
    result = service.scan([pair], edited, now=NOW)
    assert result.opportunities == []
    assert len(result.suspended_pairs) == 1
    assert result.suspended_pairs[0].status is MatchStatus.SUSPENDED
    assert result.rejections[0].reason is RejectionReason.PAIR_SUSPENDED


def test_unapproved_pair_yields_research_candidate_only() -> None:
    service, _ = build_service()
    unapproved = MarketPair(id="p2", kalshi_market_id="K1", polymarket_market_id="P1")
    result = service.scan([unapproved], markets(), now=NOW)
    assert result.confirmed == []
    assert len(result.research_candidates) == 1
    candidate = result.research_candidates[0]
    assert candidate.label is OpportunityLabel.RESEARCH_CANDIDATE
    assert RiskFlag.UNAPPROVED_PAIR in candidate.risk_flags


def test_research_candidates_can_be_switched_off() -> None:
    store = BookStore(FreshnessPolicy(max_age_ms=2000))
    store.put(kalshi_book(NOW))
    store.put(poly_book(NOW))
    strict = SETTINGS.model_copy(update={"include_research_candidates": False})
    service = OpportunityService(store, strict)
    unapproved = MarketPair(id="p2", kalshi_market_id="K1", polymarket_market_id="P1")
    result = service.scan([unapproved], markets(), now=NOW)
    assert result.opportunities == []
    assert result.rejections[0].reason is RejectionReason.PAIR_NOT_APPROVED


def test_cooldown_suppresses_a_repeated_alert() -> None:
    """A still-fresh, still-profitable book must not re-alert inside the cooldown."""
    service, store = build_service()
    pair = approved_pair()
    first = service.scan([pair], markets(), now=NOW)
    assert len(first.opportunities) == 1

    # Refresh the books so staleness cannot mask the cooldown behaviour.
    later = NOW + timedelta(seconds=10)
    store.put(kalshi_book(later))
    store.put(poly_book(later))

    second = service.scan([pair], markets(), now=later)
    assert second.opportunities == [], "must not re-announce inside the cooldown"

    # But it was still detected. The cooldown governs alerting, not observation: a
    # multi-week trial that recorded only the first sighting could not tell a persistent
    # opportunity from a one-off, which is what separates a real edge from a stale quote.
    assert len(second.alert_suppressed) == 1
    suppressed = second.alert_suppressed[0]
    assert suppressed.net_profit > 0
    assert suppressed.executable_quantity > 0
    assert len(second.all_detected) == 1


def test_a_suppressed_detection_keeps_its_full_economics() -> None:
    """It used to become a bare rejection, discarding size, prices and profit."""
    service, store = build_service()
    pair = approved_pair()
    first = service.scan([pair], markets(), now=NOW)
    original = first.opportunities[0]

    later = NOW + timedelta(seconds=10)
    store.put(kalshi_book(later))
    store.put(poly_book(later))
    suppressed = service.scan([pair], markets(), now=later).alert_suppressed[0]

    assert suppressed.dedup_key == original.dedup_key
    assert suppressed.executable_quantity == original.executable_quantity
    assert suppressed.net_profit == original.net_profit
    assert suppressed.kalshi_leg.vwap == original.kalshi_leg.vwap
    assert suppressed.polymarket_leg.vwap == original.polymarket_leg.vwap


def test_cooldown_expires() -> None:
    service, store = build_service()
    pair = approved_pair()
    service.scan([pair], markets(), now=NOW)

    later = NOW + timedelta(seconds=SETTINGS.alert_cooldown_seconds + 1)
    store.put(kalshi_book(later))
    store.put(poly_book(later))
    assert len(service.scan([pair], markets(), now=later).opportunities) == 1


def test_both_directions_are_always_evaluated() -> None:
    """Direction B is unprofitable here, but it must appear as an explicit rejection."""
    service, _ = build_service()
    result = service.scan([approved_pair()], markets(), now=NOW)
    directions = {o.direction for o in result.opportunities} | {
        r.direction for r in result.rejections
    }
    assert len(directions) == 2


def test_rejections_carry_actionable_detail() -> None:
    service, _ = build_service(book_age_ms=9000)
    result = service.scan([approved_pair()], markets(), now=NOW)
    assert all(r.detail for r in result.rejections)
