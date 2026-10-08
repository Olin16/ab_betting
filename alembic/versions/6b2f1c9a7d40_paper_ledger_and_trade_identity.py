"""Paper cash ledger, stable trade ids, and a separate execution status

Revision ID: 6b2f1c9a7d40
Revises: 2484faa40d3a

Three defects in the paper-trading record are addressed together, because they are one
problem: the portfolio could not be reconstructed after a restart.

**A cash ledger.** Balances were held only in process memory, so every restart reset them
to the configured starting figures while the trade history stayed on disk — the money and
the trades disagreed, and neither could be reconciled. ``paper_ledger_entries`` records
each movement, and each hold, keyed by a deterministic ``entry_id`` so replaying the table
is idempotent. Balances are derived from it rather than stored.

**A stable trade id.** ``paper_trades`` keyed rows only by an autoincrementing surrogate,
so the simulator's own uuid was reachable only inside the ``orders`` JSON blob and nothing
stopped the same trade being written twice. ``trade_id`` is backfilled from that blob —
the value was already there, so nothing is invented — and made unique.

**A separate execution status.** Recording a settlement overwrote ``status``, which was
also the only record of how the trade filled, so settling a partially-hedged trade removed
it from the partially-hedged count and improved the hedge-success rate purely because time
had passed. ``execution_status`` holds the fill classification.

Existing rows backfill ``execution_status`` from ``status``, except where ``status`` is
already ``settled``: those rows had their fill classification overwritten before this
column existed and it is not recoverable, so they backfill to ``pending``, which reads as
"not recorded". Inferring a hedge outcome for them would put invented classifications into
the very counts this column exists to protect.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b2f1c9a7d40"
down_revision: str | None = "2484faa40d3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _trade_id_from_orders(raw: object) -> str:
    """Read the uuid the simulator already stored inside the ``orders`` payload.

    SQLite hands back the JSON column as text and PostgreSQL as a decoded object, so both
    shapes are accepted. A row whose payload has no id keeps an empty string rather than
    receiving a generated one: a synthesized id would be indistinguishable from a real one
    while matching nothing.
    """
    payload = raw
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return ""
    if isinstance(payload, dict):
        value = payload.get("id")
        if isinstance(value, str):
            return value
    return ""


def upgrade() -> None:
    op.create_table(
        "paper_ledger_entries",
        sa.Column("entry_id", sa.String(length=160), nullable=False),
        sa.Column("account", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.String(length=40), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    op.create_index(
        "ix_paper_ledger_created_at", "paper_ledger_entries", ["created_at"], unique=False
    )

    with op.batch_alter_table("paper_trades", schema=None) as batch:
        batch.add_column(
            sa.Column("trade_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("execution_status", sa.String(length=32), nullable=False, server_default="")
        )

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, orders, status FROM paper_trades")).fetchall()
    for row in rows:
        trade_id = _trade_id_from_orders(row.orders)
        execution_status = "pending" if row.status == "settled" else row.status
        connection.execute(
            sa.text(
                "UPDATE paper_trades SET trade_id = :trade_id, "
                "execution_status = :execution_status WHERE id = :id"
            ),
            {"trade_id": trade_id, "execution_status": execution_status, "id": row.id},
        )

    with op.batch_alter_table("paper_trades", schema=None) as batch:
        # Uniqueness is added after the backfill, not before: rows whose payload carried
        # no id keep an empty string, and more than one of those must not collide.
        batch.create_index("ix_paper_trades_created_at", ["created_at"], unique=False)
        batch.create_index(
            "uq_paper_trade_id",
            ["trade_id"],
            unique=True,
            sqlite_where=sa.text("trade_id != ''"),
            postgresql_where=sa.text("trade_id != ''"),
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_trades", schema=None) as batch:
        batch.drop_index("uq_paper_trade_id")
        batch.drop_index("ix_paper_trades_created_at")
        batch.drop_column("execution_status")
        batch.drop_column("trade_id")

    op.drop_index("ix_paper_ledger_created_at", table_name="paper_ledger_entries")
    op.drop_table("paper_ledger_entries")
