"""Record cooldown-suppressed detections

Revision ID: 2484faa40d3a
Revises: 8c41f0b7d2ae

The alert cooldown suppressed an opportunity from the *record*, not just from alerting. A
repeat detection inside the window was written as a bare rejection, discarding its size,
prices and profit, so over a multi-week trial a persistent opportunity read as one
detection followed by a run of suppressions. How long an opportunity persists is exactly
what separates a real edge from a stale-quote artifact, and that was unmeasurable.

Adds ``alert_suppressed``. Every detection is now recorded; the flag says whether it was
announced. Filter ``alert_suppressed = false`` to count alerts, or ignore it to count
observations.

Existing rows default to ``false``. That is accurate for them: before this change only
announced detections reached the table, so every stored row was an announcement. No
historical data is invented — the suppressed detections of past runs were never written
and cannot be recovered.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2484faa40d3a"
down_revision: str | None = "8c41f0b7d2ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch:
        batch.add_column(
            sa.Column(
                "alert_suppressed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch:
        batch.drop_column("alert_suppressed")
