"""Identity keys for markets.

A market's identity is ``(venue, product, market_id)``, never ``(venue, market_id)``.
Polymarket Global and Polymarket US share ``Venue.POLYMARKET`` but are different venues
with different operators, identifier spaces, fee models and settlement assets. Keying
without the product let a Global market silently answer a lookup for a US market id, which
would price a leg against the wrong contract's book.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from event_arb.domain.enums import Venue, VenueProduct

if TYPE_CHECKING:
    from event_arb.domain.models import MarketPair, VenueMarket

__all__ = ["MarketKey", "kalshi_key", "market_key", "pair_market_keys", "polymarket_key"]

#: ``(venue, product, market_id)``.
MarketKey = tuple[Venue, VenueProduct, str]


def market_key(market: VenueMarket) -> MarketKey:
    """The key a market stores itself under."""
    return (market.venue, market.product, market.venue_market_id)


def kalshi_key(market_id: str) -> MarketKey:
    """Kalshi has a single product, so its product component is always ``NONE``."""
    return (Venue.KALSHI, VenueProduct.NONE, market_id)


def polymarket_key(product: VenueProduct, market_id: str) -> MarketKey:
    return (Venue.POLYMARKET, product, market_id)


def pair_market_keys(pair: MarketPair) -> tuple[MarketKey, MarketKey]:
    """The two keys a pair's legs resolve to, in (Kalshi, Polymarket) order."""
    return (
        kalshi_key(pair.kalshi_market_id),
        polymarket_key(pair.polymarket_product, pair.polymarket_market_id),
    )
