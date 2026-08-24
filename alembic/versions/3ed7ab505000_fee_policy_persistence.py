"""fee policy persistence

Adds the fee fields the calculation path needs but never stored: the rounding policy and
the maker rebate rate.

**No historical fee data is invented.** Rows written before these columns existed have no
recorded policy, and are left as ``unknown``. The dispatcher refuses to price an unknown
policy, so such a market must be re-fetched from the venue before it can produce an
opportunity. Backfilling a plausible policy by venue would be a guess about how a fee was
rounded, and a wrong guess there is silent: both roundings return believable numbers.

The maker rebate defaults to zero, which is the true charge for every venue that does not
pay one; the only venue that does (Polymarket US) will repopulate it on the next refresh
along with its policy.

Revision ID: 3ed7ab505000
Revises: c94468f4068e
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3ed7ab505000"
down_revision: str | None = "c94468f4068e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "venue_markets",
        sa.Column("fee_policy", sa.String(length=48), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "venue_markets",
        sa.Column(
            "fee_maker_rebate_rate",
            sa.String(length=40),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("venue_markets", "fee_maker_rebate_rate")
    op.drop_column("venue_markets", "fee_policy")
