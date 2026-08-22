"""Arbitrage profit calculation for an approved (or candidate) market pair.

For a settlement-equivalent binary pair, holding one YES contract on one venue and one NO
contract on the other pays exactly $1.00 per unit at settlement, whichever way the event
resolves. The two directions are::

    Direction A: buy Kalshi YES  + buy Polymarket NO
    Direction B: buy Kalshi NO   + buy Polymarket YES

and for quantity ``q``::

    guaranteed_payout = q * $1.00

    net_profit = guaranteed_payout
               - executable_cost_of_leg_1     (VWAP from real depth, not midpoint)
               - executable_cost_of_leg_2
               - venue_fees                   (per-venue, per-fill, per-market config)
               - slippage_buffer
               - operational_cost_buffer
               - usdc_conversion_allowance

Two things are deliberately *not* assumed:

- that the payout is worth $1.00 of USD. The Polymarket leg settles in USDC, so a
  configurable haircut prices conversion and depeg risk;
- that this is risk-free. Even a perfect calculation here describes a *non-atomic*
  two-leg execution. See :mod:`event_arb.opportunities.risk` and docs/risk-model.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import gcd

from event_arb.config import OpportunitySettings
from event_arb.domain.enums import (
    ArbDirection,
    OpportunityLabel,
    Outcome,
    RejectionReason,
    RiskFlag,
    SettlementAsset,
    Venue,
)
from event_arb.domain.models import (
    BookLevel,
    LegPlan,
    MarketPair,
    Opportunity,
    OrderBook,
    RejectedOpportunity,
    VenueMarket,
)
from event_arb.domain.money import (
    CENT_EXP,
    ONE_DOLLAR,
    QTY_EXP,
    ZERO,
    ceil_to,
    floor_to,
    quantize_price,
    quantize_qty,
)
from event_arb.opportunities.depth import DepthWalk, walk_asks
from event_arb.venues.kalshi.fees import KalshiOrderFeeAccumulator
from event_arb.venues.polymarket.fees import taker_fee

__all__ = ["CalculationInputs", "calculate_direction"]


@dataclass(frozen=True)
class CalculationInputs:
    """Everything needed to evaluate one direction of one pair."""

    pair: MarketPair
    kalshi_market: VenueMarket
    polymarket_market: VenueMarket
    kalshi_book: OrderBook
    polymarket_book: OrderBook
    settings: OpportunitySettings
    kalshi_book_age_ms: int
    polymarket_book_age_ms: int
    usdc_haircut_pct: Decimal = Decimal("0.10")
    kalshi_direct_member: bool = False
    risk_flags: tuple[RiskFlag, ...] = ()


def _leg_outcomes(direction: ArbDirection, inverted: bool) -> tuple[Outcome, Outcome]:
    """Resolve which outcome to buy on each venue.

    ``inverted`` handles pairs where Polymarket's YES token corresponds to Kalshi's NO
    (for example "Team A wins" versus "Team B wins"). Getting this backwards would buy
    the same side twice — a doubled directional bet, not a hedge — so it is explicit on
    the pair and never inferred at calculation time.
    """
    kalshi_outcome = direction.kalshi_outcome
    polymarket_outcome = direction.polymarket_outcome
    if inverted:
        polymarket_outcome = polymarket_outcome.opposite
    return kalshi_outcome, polymarket_outcome


def _kalshi_fees(walk: DepthWalk, market: VenueMarket, direct_member: bool) -> Decimal:
    """Fee for the whole Kalshi order, charged per fill through one accumulator."""
    accumulator = KalshiOrderFeeAccumulator(config=market.fee_config, direct_member=direct_member)
    return sum(
        (accumulator.charge_fill(fill.quantity, fill.price).net for fill in walk.fills), ZERO
    )


def _polymarket_fees(walk: DepthWalk, market: VenueMarket) -> Decimal:
    return sum(
        (taker_fee(fill.quantity, fill.price, market.fee_config) for fill in walk.fills), ZERO
    )


@dataclass(frozen=True)
class _QuantityEvaluation:
    """Full economics of executing one candidate quantity."""

    quantity: Decimal
    kalshi_walk: DepthWalk
    polymarket_walk: DepthWalk
    kalshi_fees: Decimal
    polymarket_fees: Decimal
    gross_cost: Decimal
    expected_payout: Decimal
    slippage: Decimal
    operational: Decimal
    usdc_allowance: Decimal
    net_profit: Decimal
    return_on_capital: Decimal


def _candidate_quantities(
    kalshi_asks: tuple[BookLevel, ...],
    polymarket_asks: tuple[BookLevel, ...],
    max_quantity: Decimal,
    kalshi_increment: Decimal,
    polymarket_increment: Decimal,
) -> list[Decimal]:
    """Quantities worth evaluating: every order-book level boundary, capped.

    Net profit is a concave piecewise-linear function of quantity — payout grows linearly
    while cost grows convexly as each leg eats into worse levels — so its maximum lies at
    a level boundary. Evaluating only the deepest fillable size (the previous behaviour)
    misses a profitable prefix sitting behind unprofitable depth.
    """
    increments = [
        max(1, int(increment / QTY_EXP)) for increment in (kalshi_increment, polymarket_increment)
    ]
    common_units = increments[0] * increments[1] // gcd(*increments)
    common_increment = Decimal(common_units) * QTY_EXP

    def executable_grid(value: Decimal) -> Decimal:
        units = floor_to(value / common_increment, Decimal("1"))
        return quantize_qty(units * common_increment)

    boundaries: set[Decimal] = set()
    for asks in (kalshi_asks, polymarket_asks):
        cumulative = ZERO
        for level in asks:
            cumulative += level.quantity
            capped = executable_grid(min(cumulative, max_quantity))
            if capped > ZERO:
                boundaries.add(capped)
            if cumulative >= max_quantity:
                break

    capped_max = executable_grid(max_quantity)
    if capped_max > ZERO:
        boundaries.add(capped_max)
    return sorted(boundaries)


def _evaluate_quantity(
    inputs: CalculationInputs,
    quantity: Decimal,
    kalshi_asks: tuple[BookLevel, ...],
    polymarket_asks: tuple[BookLevel, ...],
    kalshi_ceiling: Decimal,
    polymarket_ceiling: Decimal,
) -> _QuantityEvaluation | None:
    """Cost out one candidate quantity, or ``None`` if both legs cannot fill it."""
    settings = inputs.settings

    kalshi_walk = walk_asks(
        kalshi_asks,
        quantity,
        tick_size=inputs.kalshi_market.tick_size,
        max_price=kalshi_ceiling,
        min_quantity_increment=inputs.kalshi_market.min_quantity_increment,
    )
    polymarket_walk = walk_asks(
        polymarket_asks,
        quantity,
        tick_size=inputs.polymarket_market.tick_size,
        max_price=polymarket_ceiling,
        min_quantity_increment=inputs.polymarket_market.min_quantity_increment,
    )
    # Unequal leg sizes are an unhedged bet, not an arbitrage.
    if kalshi_walk.filled_quantity != quantity or polymarket_walk.filled_quantity != quantity:
        return None

    kalshi_fees = ceil_to(
        _kalshi_fees(kalshi_walk, inputs.kalshi_market, inputs.kalshi_direct_member), CENT_EXP
    )
    polymarket_fees = _polymarket_fees(polymarket_walk, inputs.polymarket_market)

    gross_cost = kalshi_walk.gross_cost + polymarket_walk.gross_cost
    expected_payout = quantity * ONE_DOLLAR
    slippage = gross_cost * (settings.slippage_buffer_pct / Decimal("100"))
    operational = settings.operational_cost_usd

    # The Polymarket leg pays out in USDC; converting it back to USD is neither free nor
    # guaranteed at par. Charge the haircut against the portion of the payout that
    # arrives as USDC.
    usdc_allowance = ZERO
    if inputs.polymarket_market.settlement_asset is SettlementAsset.USDC:
        usdc_allowance = expected_payout * (inputs.usdc_haircut_pct / Decimal("100"))

    net_profit = (
        expected_payout
        - gross_cost
        - kalshi_fees
        - polymarket_fees
        - slippage
        - operational
        - usdc_allowance
    )
    return_on_capital = (net_profit / gross_cost) * Decimal("100") if gross_cost > ZERO else ZERO

    return _QuantityEvaluation(
        quantity=quantity,
        kalshi_walk=kalshi_walk,
        polymarket_walk=polymarket_walk,
        kalshi_fees=kalshi_fees,
        polymarket_fees=polymarket_fees,
        gross_cost=gross_cost,
        expected_payout=expected_payout,
        slippage=slippage,
        operational=operational,
        usdc_allowance=usdc_allowance,
        net_profit=net_profit,
        return_on_capital=return_on_capital,
    )


def calculate_direction(
    inputs: CalculationInputs, direction: ArbDirection
) -> Opportunity | RejectedOpportunity:
    """Evaluate one direction, returning either an opportunity or a typed rejection."""
    pair = inputs.pair
    settings = inputs.settings

    def reject(reason: RejectionReason, detail: str = "") -> RejectedOpportunity:
        return RejectedOpportunity(
            pair_id=pair.id, direction=direction, reason=reason, detail=detail
        )

    if not (inputs.kalshi_market.is_tradable and inputs.polymarket_market.is_tradable):
        return reject(RejectionReason.MARKET_NOT_TRADABLE, "one or both markets are not tradable")

    kalshi_outcome, polymarket_outcome = _leg_outcomes(direction, pair.outcomes_inverted)
    kalshi_asks = inputs.kalshi_book.side(kalshi_outcome).asks
    polymarket_asks = inputs.polymarket_book.side(polymarket_outcome).asks

    if not kalshi_asks or not polymarket_asks:
        return reject(RejectionReason.BOOK_INCOMPLETE, "no ask liquidity on at least one leg")

    # A combined best-ask cost at or above $1.00 cannot produce a profit, whatever the
    # depth looks like. Reject before doing the more expensive work.
    best_combined = kalshi_asks[0].price + polymarket_asks[0].price
    if best_combined >= ONE_DOLLAR:
        return reject(
            RejectionReason.NO_EXECUTABLE_QUANTITY,
            f"best combined ask {best_combined} >= $1.00",
        )

    # The most we could ever want is the configured cap; depth decides the rest. Each leg
    # is walked with a price ceiling so we never "fill" at a level that alone would
    # exhaust the spread.
    requested = quantize_qty(settings.max_quantity)
    kalshi_ceiling = ONE_DOLLAR - polymarket_asks[0].price
    polymarket_ceiling = ONE_DOLLAR - kalshi_asks[0].price

    candidates = _candidate_quantities(
        kalshi_asks,
        polymarket_asks,
        requested,
        inputs.kalshi_market.min_quantity_increment,
        inputs.polymarket_market.min_quantity_increment,
    )
    if not candidates:
        return reject(RejectionReason.NO_EXECUTABLE_QUANTITY, "no overlapping executable size")

    evaluations = [
        evaluation
        for quantity in candidates
        if (
            evaluation := _evaluate_quantity(
                inputs,
                quantity,
                kalshi_asks,
                polymarket_asks,
                kalshi_ceiling,
                polymarket_ceiling,
            )
        )
        is not None
    ]
    if not evaluations:
        return reject(
            RejectionReason.NO_EXECUTABLE_QUANTITY, "legs could not be filled at a common quantity"
        )

    qualifying = [
        evaluation
        for evaluation in evaluations
        if evaluation.net_profit >= settings.min_net_profit_usd
        and evaluation.return_on_capital >= settings.min_return_pct
        and evaluation.net_profit > ZERO
    ]
    if not qualifying:
        # Report against the best available size so the reason describes the real
        # obstacle rather than an arbitrary depth.
        best = max(evaluations, key=lambda item: item.net_profit)
        if best.net_profit <= ZERO:
            return reject(
                RejectionReason.NEGATIVE_AFTER_FEES,
                f"net profit {quantize_price(best.net_profit)} after fees and buffers "
                f"at the best executable size {best.quantity}",
            )
        if best.net_profit < settings.min_net_profit_usd:
            return reject(
                RejectionReason.BELOW_MIN_PROFIT,
                f"net profit {quantize_price(best.net_profit)} < minimum "
                f"{settings.min_net_profit_usd}",
            )
        return reject(
            RejectionReason.BELOW_MIN_RETURN,
            f"return {quantize_price(best.return_on_capital)}% < minimum "
            f"{settings.min_return_pct}%",
        )

    chosen = max(qualifying, key=lambda item: item.net_profit)
    executable = chosen.quantity
    kalshi_walk = chosen.kalshi_walk
    polymarket_walk = chosen.polymarket_walk
    kalshi_fees = chosen.kalshi_fees
    polymarket_fees = chosen.polymarket_fees
    gross_cost = chosen.gross_cost
    expected_payout = chosen.expected_payout
    slippage = chosen.slippage
    operational = chosen.operational
    usdc_allowance = chosen.usdc_allowance
    net_profit = chosen.net_profit
    return_on_capital = chosen.return_on_capital

    kalshi_leg = LegPlan(
        venue=Venue.KALSHI,
        market_id=inputs.kalshi_market.venue_market_id,
        outcome=kalshi_outcome,
        quantity=executable,
        vwap=kalshi_walk.vwap,
        worst_price=kalshi_walk.worst_price,
        levels_consumed=kalshi_walk.levels_consumed,
        gross_cost=quantize_price(kalshi_walk.gross_cost),
        fees=kalshi_fees,
        settlement_asset=inputs.kalshi_market.settlement_asset,
        book_age_ms=inputs.kalshi_book_age_ms,
    )
    polymarket_leg = LegPlan(
        venue=Venue.POLYMARKET,
        market_id=inputs.polymarket_market.venue_market_id,
        outcome=polymarket_outcome,
        quantity=executable,
        vwap=polymarket_walk.vwap,
        worst_price=polymarket_walk.worst_price,
        levels_consumed=polymarket_walk.levels_consumed,
        gross_cost=quantize_price(polymarket_walk.gross_cost),
        fees=quantize_price(polymarket_fees),
        settlement_asset=inputs.polymarket_market.settlement_asset,
        book_age_ms=inputs.polymarket_book_age_ms,
    )

    return Opportunity(
        pair_id=pair.id,
        direction=direction,
        label=pair.label,
        requested_quantity=requested,
        executable_quantity=executable,
        kalshi_leg=kalshi_leg,
        polymarket_leg=polymarket_leg,
        gross_acquisition_cost=quantize_price(gross_cost),
        kalshi_fees=kalshi_fees,
        polymarket_fees=quantize_price(polymarket_fees),
        slippage_allowance=quantize_price(slippage),
        operational_cost_allowance=quantize_price(operational),
        usdc_conversion_allowance=quantize_price(usdc_allowance),
        expected_payout=quantize_price(expected_payout),
        net_profit=quantize_price(net_profit),
        return_on_capital=quantize_price(return_on_capital),
        gross_spread=quantize_price(expected_payout - gross_cost),
        risk_flags=inputs.risk_flags,
    )


def label_for(pair: MarketPair) -> OpportunityLabel:
    """Public helper: only an approved pair is ever ``confirmed``."""
    return pair.label
