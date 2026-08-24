"""product identity

Adds an explicit product discriminator so Polymarket Global and Polymarket US stop
collapsing into one venue identity.

Backfill is deliberately conservative: every existing Polymarket row becomes
``polymarket_global``, because all data written before this migration came from the Global
adapter. Guessing "US" for anything would be inventing provenance, and the whole point of
the discriminator is that a Global approval must never satisfy a US pair.

Kalshi rows become ``none``: the venue has one product, so there is nothing to discriminate.

Revision ID: 0d514772d2da
Revises: f56b63cbaa9d
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0d514772d2da"
down_revision: str | None = "f56b63cbaa9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Conservative values applied to rows written before product identity existed.
_GLOBAL = "polymarket_global"
_NONE = "none"


def upgrade() -> None:
    # server_default lets the NOT NULL columns land on tables that already have rows;
    # the explicit backfill below then sets the correct value per venue.
    op.add_column(
        "venue_markets",
        sa.Column("product", sa.String(length=32), nullable=False, server_default=_NONE),
    )
    op.add_column(
        "venue_markets",
        sa.Column("settlement_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orderbook_snapshots",
        sa.Column("product", sa.String(length=32), nullable=False, server_default=_NONE),
    )
    op.add_column(
        "market_pairs",
        sa.Column(
            "polymarket_product", sa.String(length=32), nullable=False, server_default=_GLOBAL
        ),
    )

    # Backfill: anything Polymarket predates the US adapter, so it is Global.
    op.execute(
        sa.text("UPDATE venue_markets SET product = :g WHERE venue = 'POLYMARKET'").bindparams(
            g=_GLOBAL
        )
    )
    op.execute(
        sa.text(
            "UPDATE orderbook_snapshots SET product = :g WHERE venue = 'POLYMARKET'"
        ).bindparams(g=_GLOBAL)
    )

    # Product joins market identity. batch mode is required for SQLite, which cannot
    # alter constraints in place.
    with op.batch_alter_table("venue_markets") as batch:
        batch.drop_constraint("uq_venue_market", type_="unique")
        batch.create_unique_constraint("uq_venue_market", ["venue", "product", "venue_market_id"])


def downgrade() -> None:
    with op.batch_alter_table("venue_markets") as batch:
        batch.drop_constraint("uq_venue_market", type_="unique")
        batch.create_unique_constraint("uq_venue_market", ["venue", "venue_market_id"])

    op.drop_column("market_pairs", "polymarket_product")
    op.drop_column("orderbook_snapshots", "product")
    op.drop_column("venue_markets", "settlement_deadline")
    op.drop_column("venue_markets", "product")
