"""Include product in the snapshot index

Revision ID: 3ea85fd4182a
Revises: 3ed7ab505000
Create Date: 2026-08-24 11:23:28.023656

``ix_books_venue_market_time`` covered ``(venue, market_id, received_at)``. The two
Polymarket products share a venue value, so their snapshot histories interleaved under one
market id and a range read could not tell them apart.

Rebuilding the index changes no rows. Existing Polymarket snapshots keep whatever product
they were written with — which, before this release, was always ``"none"`` because the
repository defaulted it and no caller overrode it. **Those rows are not corrected here**:
we cannot know which product produced them without knowing the scanner's configuration at
write time, and guessing would file a Global book as US. They remain readable under
``product = 'none'`` and should be treated as unattributed history, not as either product.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3ea85fd4182a"
down_revision: str | None = "3ed7ab505000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ix_books_venue_market_time"
_TABLE = "orderbook_snapshots"


def upgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
    op.create_index(_NAME, _TABLE, ["venue", "product", "market_id", "received_at"])


def downgrade() -> None:
    op.drop_index(_NAME, table_name=_TABLE)
    op.create_index(_NAME, _TABLE, ["venue", "market_id", "received_at"])
