"""Simulated cash accounts, keyed by where the money actually sits.

A balance is not a number per currency. It is a number per *account*, and an account is
``(venue, product, settlement asset)``. Polymarket US and Kalshi both settle in USD, but
USD sitting at Kalshi cannot pay for a Polymarket US leg: moving it is a withdrawal, a
transfer and a deposit, none of which this system does or simulates. Tracking one pooled
``USD`` number therefore let a trade spend money it did not have at the venue it was
spending at, and reported a balance no operator could act on.

The asset dimension survives too, because USD and USDC carry conversion and depeg risk
between them even when both sit at Polymarket.
"""

from __future__ import annotations

from dataclasses import dataclass

from event_arb.domain.enums import SettlementAsset, Venue, VenueProduct

__all__ = ["AccountId", "account_for"]


@dataclass(frozen=True, order=True)
class AccountId:
    """One simulated pot of money at one venue product, in one asset."""

    venue: Venue
    product: VenueProduct
    asset: SettlementAsset

    @property
    def venue_key(self) -> str:
        """The venue half of the key.

        Kalshi has a single product, so it names itself. A Polymarket leg names its
        product, because Global and US are different venues that share a brand.
        """
        if self.product is VenueProduct.NONE:
            return self.venue.value.lower()
        return self.product.value

    @property
    def key(self) -> str:
        """Stable string identity, used in configuration, storage and reports.

        ``kalshi:USD``, ``polymarket_us:USD``, ``polymarket_global:USDC``.
        """
        return f"{self.venue_key}:{self.asset.value}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key

    @classmethod
    def parse(cls, key: str) -> AccountId:
        """Rebuild an account from its :attr:`key`.

        Deliberately strict. An unparseable key is a configuration error the operator has
        to see, not a reason to invent an account that then silently holds no money.
        """
        venue_key, _, asset_value = key.partition(":")
        if not venue_key or not asset_value:
            raise ValueError(
                f"account key {key!r} must look like 'kalshi:USD' or 'polymarket_us:USD'"
            )
        try:
            asset = SettlementAsset(asset_value)
        except ValueError as exc:
            raise ValueError(
                f"account key {key!r} names an unknown settlement asset {asset_value!r}; "
                f"expected one of {sorted(a.value for a in SettlementAsset)}"
            ) from exc

        if venue_key == Venue.KALSHI.value.lower():
            return cls(Venue.KALSHI, VenueProduct.NONE, asset)
        if venue_key == Venue.POLYMARKET.value.lower():
            return cls(Venue.POLYMARKET, VenueProduct.NONE, asset)
        try:
            product = VenueProduct(venue_key)
        except ValueError as exc:
            raise ValueError(
                f"account key {key!r} names an unknown venue or product {venue_key!r}; "
                "expected 'kalshi', 'polymarket_us' or 'polymarket_global'"
            ) from exc
        if not product.is_polymarket:
            raise ValueError(f"account key {key!r} names a product that is not a venue")
        return cls(Venue.POLYMARKET, product, asset)


def account_for(venue: Venue, product: VenueProduct, asset: SettlementAsset) -> AccountId:
    """The account a leg on ``venue``/``product`` settling in ``asset`` draws on.

    Kalshi's product component is normalized to ``NONE`` because it has exactly one
    product; a Polymarket leg keeps whatever product it carries. A Polymarket leg with
    ``NONE`` gets its own account rather than being folded into either real product —
    money must never pool across products because a leg was recorded without one.
    """
    if venue is Venue.KALSHI:
        return AccountId(Venue.KALSHI, VenueProduct.NONE, asset)
    return AccountId(venue, product, asset)
