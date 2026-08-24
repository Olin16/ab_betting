"""Sportsbook quote vs exchange contract — research-candidate comparison.

**Paper only. Nothing here can place a bet or submit an order.**

The shape of the trade: a sportsbook quote pays out on one side of an event, and an
exchange contract on the *opposite* side pays $1.00 if that side wins. Hold both and one of
them pays whatever happens.

For a stake ``S`` at decimal odds ``D``, and ``Q`` exchange contracts bought at price ``P``:

    outlay              = S + Q*P + fees + buffers
    if selection wins   = S*D          (the exchange contract expires worthless)
    if selection loses  = Q * $1.00    (the bet loses)

    guaranteed = min(S*D, Q) - outlay

Payouts are computed on both branches and the **minimum** taken. Comparing implied
probabilities instead would be a different and wrong calculation: implied probability
includes the book's margin, and summing two margins tells you nothing about what you would
actually be paid.

Five reasons nothing here is ever called arbitrage:

1. Execution at the book is a **manual human action**, at a price that can move.
2. The book may refuse, limit, or void the bet after acceptance.
3. Without a published stake limit, executable size is unknown.
4. Push, void, overtime and grading rules can pay both legs the same way.
5. The mapping is a research candidate until a human approves settlement equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from event_arb.domain.enums import Outcome, RejectionReason, SettlementAsset, Venue
from event_arb.domain.models import OrderBook, VenueMarket
from event_arb.domain.money import ONE_DOLLAR, ZERO, floor_to, quantize_price, quantize_qty
from event_arb.opportunities.depth import walk_asks
from event_arb.sportsbook.base import QuoteFreshnessPolicy
from event_arb.sportsbook.enums import QuoteStatus, SportsbookRiskFlag
from event_arb.sportsbook.models import SportsbookQuote

__all__ = [
    "SportsbookComparison",
    "SportsbookComparisonSettings",
    "SportsbookRejection",
    "compare_quote_to_contract",
]


@dataclass(frozen=True)
class SportsbookComparisonSettings:
    """Thresholds and allowances. All are floors on disclosed risk, not guarantees."""

    min_net_profit_usd: Decimal = Decimal("1.00")
    min_return_pct: Decimal = Decimal("1.0")
    #: Price movement expected between seeing a quote and a human placing the bet.
    manual_execution_slippage_pct: Decimal = Decimal("2.0")
    #: Flat allowance for operational cost of a manual leg.
    operational_cost_usd: Decimal = Decimal("0.50")
    #: Exchange-side fee-inclusive buffer, as a percentage of exchange outlay.
    exchange_slippage_pct: Decimal = Decimal("0.5")
    #: Applied when the exchange leg settles in a different asset than the bet.
    settlement_asset_haircut_pct: Decimal = Decimal("0.10")
    #: Stake assumed when the provider published no limit. Used only to *illustrate*;
    #: the result is always flagged liquidity-unverified and never treated as executable.
    assumed_stake_without_limit: Decimal = Decimal("100")
    max_quote_age_ms: int = 30_000
    max_book_age_ms: int = 5_000
    #: Optional flat tax/withholding on net winnings, if the operator's regime applies one.
    winnings_tax_pct: Decimal = ZERO


@dataclass(frozen=True)
class SportsbookRejection:
    """Why a comparison did not qualify."""

    reason: RejectionReason
    detail: str
    quote_key: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class SportsbookComparison:
    """A research candidate: a sportsbook quote against an exchange contract.

    Never labeled arbitrage. :attr:`is_liquidity_verified` is false whenever the stake was
    assumed rather than published.
    """

    quote: SportsbookQuote
    exchange_venue: Venue
    exchange_market_id: str
    exchange_outcome: Outcome
    stake: Decimal
    decimal_odds: Decimal
    exchange_quantity: Decimal
    exchange_vwap: Decimal
    sportsbook_outlay: Decimal
    exchange_outlay: Decimal
    exchange_fees: Decimal
    manual_slippage_allowance: Decimal
    operational_cost_allowance: Decimal
    settlement_asset_allowance: Decimal
    tax_allowance: Decimal
    payout_if_selection_wins: Decimal
    payout_if_selection_loses: Decimal
    guaranteed_payout: Decimal
    total_outlay: Decimal
    net_profit: Decimal
    return_on_capital: Decimal
    risk_flags: tuple[SportsbookRiskFlag, ...]
    quote_age_ms: int
    book_age_ms: int
    is_liquidity_verified: bool
    mapping_approved: bool
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def label(self) -> str:
        """Always a research candidate. There is no confirmed label on this path."""
        return "research_candidate"

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "sportsbook": self.quote.sportsbook,
            "provider": self.quote.provider.value,
            "selection": self.quote.selection,
            "line": None if self.quote.line is None else str(self.quote.line),
            "decimal_odds": str(self.decimal_odds),
            "stake": str(self.stake),
            "exchange_venue": self.exchange_venue.value,
            "exchange_market_id": self.exchange_market_id,
            "exchange_outcome": self.exchange_outcome.value,
            "exchange_quantity": str(self.exchange_quantity),
            "exchange_vwap": str(self.exchange_vwap),
            "total_outlay": str(self.total_outlay),
            "guaranteed_payout": str(self.guaranteed_payout),
            "net_profit": str(self.net_profit),
            "return_on_capital_pct": str(self.return_on_capital),
            "is_liquidity_verified": self.is_liquidity_verified,
            "mapping_approved": self.mapping_approved,
            "risk_flags": [flag.value for flag in self.risk_flags],
            "disclaimer": (
                "Research candidate only. Not arbitrage and not risk-free: the sportsbook "
                "leg is executed manually at a price that may move, the book may refuse or "
                "void the bet, and settlement rules may differ."
            ),
        }


def compare_quote_to_contract(
    quote: SportsbookQuote,
    exchange_book: OrderBook,
    exchange_market: VenueMarket,
    exchange_outcome: Outcome,
    settings: SportsbookComparisonSettings,
    mapping_approved: bool = False,
    now: datetime | None = None,
) -> SportsbookComparison | SportsbookRejection:
    """Evaluate one quote against the opposing exchange contract.

    ``exchange_outcome`` must be the side that wins when the sportsbook selection
    **loses**. Choosing it correctly is a human judgement recorded on the mapping; getting
    it wrong buys the same side twice, which is a doubled directional bet rather than a
    hedge, so it is never inferred here.
    """
    reference = now or datetime.now(UTC)

    def reject(reason: RejectionReason, detail: str) -> SportsbookRejection:
        return SportsbookRejection(
            reason=reason, detail=detail, quote_key=quote.quote_key, detected_at=reference
        )

    # --- state checks -----------------------------------------------------------
    if quote.status is QuoteStatus.SUSPENDED:
        return reject(RejectionReason.MARKET_NOT_TRADABLE, "sportsbook market is suspended")
    if quote.status is QuoteStatus.SETTLED:
        return reject(RejectionReason.MARKET_NOT_TRADABLE, "event already settled")
    if not exchange_market.is_tradable:
        return reject(RejectionReason.MARKET_NOT_TRADABLE, "exchange market is not tradable")

    quote_age = quote.age_ms(reference)
    if quote.is_stale or not QuoteFreshnessPolicy(settings.max_quote_age_ms).is_fresh(
        quote, reference
    ):
        return reject(
            RejectionReason.BOOK_STALE,
            f"quote age {quote_age}ms exceeds limit {settings.max_quote_age_ms}ms",
        )

    book_age = exchange_book.age_ms(reference)
    if exchange_book.is_stale or not exchange_book.is_complete:
        return reject(RejectionReason.BOOK_STALE, "exchange book stale or incomplete")
    if book_age > settings.max_book_age_ms:
        return reject(
            RejectionReason.BOOK_STALE,
            f"exchange book age {book_age}ms exceeds limit {settings.max_book_age_ms}ms",
        )

    # --- sizing -----------------------------------------------------------------
    decimal_odds = quote.decimal_odds
    liquidity_verified = quote.has_published_limit
    stake = (
        quote.max_stake
        if quote.max_stake is not None and quote.max_stake > ZERO
        else settings.assumed_stake_without_limit
    )
    if stake <= ZERO:
        return reject(RejectionReason.NO_EXECUTABLE_QUANTITY, "stake resolves to zero")

    # Hedge sizing: buy enough contracts that the losing branch pays what the winning
    # branch would. Rounded DOWN to the exchange quantity grid, because rounding up would
    # claim a fill the book cannot supply.
    asks = exchange_book.side(exchange_outcome).asks
    if not asks:
        return reject(RejectionReason.BOOK_INCOMPLETE, "no exchange ask liquidity")

    target_quantity = quantize_qty(stake * decimal_odds)
    increment = exchange_market.min_quantity_increment
    if increment > ZERO:
        target_quantity = quantize_qty(
            floor_to(target_quantity / increment, Decimal("1")) * increment
        )
    if target_quantity <= ZERO:
        return reject(RejectionReason.NO_EXECUTABLE_QUANTITY, "hedge size rounds to zero")

    walk = walk_asks(
        asks,
        target_quantity,
        tick_size=exchange_market.tick_size,
        min_quantity_increment=increment,
    )
    if walk.filled_quantity <= ZERO:
        return reject(RejectionReason.NO_EXECUTABLE_QUANTITY, "exchange leg cannot fill")

    quantity = walk.filled_quantity
    # Partial exchange fill means we must shrink the bet to stay hedged; an oversized
    # sportsbook leg is naked exposure on the branch the exchange no longer covers.
    if quantity < target_quantity:
        stake = quantize_price(quantity / decimal_odds)
        if stake <= ZERO:
            return reject(RejectionReason.NO_EXECUTABLE_QUANTITY, "rebalanced stake is zero")

    # --- costs ------------------------------------------------------------------
    exchange_outlay = walk.gross_cost
    exchange_fees = _exchange_fees(quantity, walk.vwap, exchange_market)

    manual_slippage = stake * (settings.manual_execution_slippage_pct / Decimal("100"))
    exchange_slippage = exchange_outlay * (settings.exchange_slippage_pct / Decimal("100"))
    operational = settings.operational_cost_usd

    settlement_allowance = ZERO
    if exchange_market.settlement_asset is not SettlementAsset.USD:
        # The bet is settled in the book's currency; a different exchange settlement asset
        # must be converted, which is neither free nor guaranteed at par.
        settlement_allowance = (quantity * ONE_DOLLAR) * (
            settings.settlement_asset_haircut_pct / Decimal("100")
        )

    payout_win = stake * decimal_odds
    payout_lose = quantity * ONE_DOLLAR
    guaranteed = min(payout_win, payout_lose)

    gross_outlay = stake + exchange_outlay
    tax = ZERO
    if settings.winnings_tax_pct > ZERO:
        taxable = max(ZERO, guaranteed - gross_outlay)
        tax = taxable * (settings.winnings_tax_pct / Decimal("100"))

    total_outlay = (
        gross_outlay
        + exchange_fees
        + manual_slippage
        + exchange_slippage
        + operational
        + settlement_allowance
        + tax
    )
    net_profit = guaranteed - total_outlay

    if net_profit <= ZERO:
        return reject(
            RejectionReason.NEGATIVE_AFTER_FEES,
            f"net {quantize_price(net_profit)} after fees, tax and buffers",
        )
    if net_profit < settings.min_net_profit_usd:
        return reject(
            RejectionReason.BELOW_MIN_PROFIT,
            f"net {quantize_price(net_profit)} < minimum {settings.min_net_profit_usd}",
        )
    return_on_capital = (
        (net_profit / gross_outlay) * Decimal("100") if gross_outlay > ZERO else ZERO
    )
    if return_on_capital < settings.min_return_pct:
        return reject(
            RejectionReason.BELOW_MIN_RETURN,
            f"return {quantize_price(return_on_capital)}% < minimum {settings.min_return_pct}%",
        )

    # --- risk flags -------------------------------------------------------------
    flags = set(quote.risk_flags())
    if not mapping_approved:
        flags.add(SportsbookRiskFlag.UNAPPROVED_MAPPING)
    if not liquidity_verified:
        flags.add(SportsbookRiskFlag.LIQUIDITY_UNVERIFIED)
    if exchange_market.settlement_source and exchange_market.settlement_source not in {
        "",
        quote.raw_metadata.get("settlement_source", ""),
    }:
        flags.add(SportsbookRiskFlag.SETTLEMENT_SOURCE_DIFFERS)
    flags.add(SportsbookRiskFlag.JURISDICTION_DEPENDENT)

    return SportsbookComparison(
        quote=quote,
        exchange_venue=exchange_market.venue,
        exchange_market_id=exchange_market.venue_market_id,
        exchange_outcome=exchange_outcome,
        stake=quantize_price(stake),
        decimal_odds=decimal_odds,
        exchange_quantity=quantity,
        exchange_vwap=walk.vwap,
        sportsbook_outlay=quantize_price(stake),
        exchange_outlay=quantize_price(exchange_outlay),
        exchange_fees=quantize_price(exchange_fees),
        manual_slippage_allowance=quantize_price(manual_slippage + exchange_slippage),
        operational_cost_allowance=quantize_price(operational),
        settlement_asset_allowance=quantize_price(settlement_allowance),
        tax_allowance=quantize_price(tax),
        payout_if_selection_wins=quantize_price(payout_win),
        payout_if_selection_loses=quantize_price(payout_lose),
        guaranteed_payout=quantize_price(guaranteed),
        total_outlay=quantize_price(total_outlay),
        net_profit=quantize_price(net_profit),
        return_on_capital=quantize_price(return_on_capital),
        risk_flags=tuple(sorted(flags, key=lambda flag: flag.value)),
        quote_age_ms=quote_age,
        book_age_ms=book_age,
        is_liquidity_verified=liquidity_verified,
        mapping_approved=mapping_approved,
        detected_at=reference,
    )


def _exchange_fees(quantity: Decimal, price: Decimal, market: VenueMarket) -> Decimal:
    """Fees on the exchange leg, using the venue's own model."""
    if market.venue is Venue.KALSHI:
        from event_arb.venues.kalshi.fees import KalshiOrderFeeAccumulator

        accumulator = KalshiOrderFeeAccumulator(config=market.fee_config)
        return accumulator.charge_fill(quantity, price).net

    from event_arb.venues.polymarket.fees import taker_fee

    return taker_fee(quantity, price, market.fee_config)
