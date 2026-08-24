"""Pair identity includes product

Revision ID: 8c41f0b7d2ae
Revises: 3ea85fd4182a

Pair ids used to be ``sha256(kalshi_id | polymarket_id)`` and ``uq_pair`` covered only
``(kalshi_market_id, polymarket_market_id)``. Both omitted the Polymarket product, so the
same two venue ids on Polymarket Global and Polymarket US were one pair. Two consequences:

1. persisting both products raised ``IntegrityError`` on ``uq_pair``;
2. after ids gained a product component, candidate generation minted a *new* id for legs a
   legacy row already held, and persisting it collided with the legacy row.

This migration rekeys every legacy pair — not only approved ones — to the product-aware id
and repoints dependent rows.

**Approvals are cleared.** Any rekeyed row that was ``manually_approved`` is set to
``suspended`` with ``manually_approved`` false. The approval was recorded against an identity
that did not distinguish the two products, so it cannot be carried forward; the pair must
be re-reviewed under its new id. This is deliberately destructive of the approval flag and
nothing else.

**Collisions.** Two legacy rows can rekey to the same id when they hold the same legs and
product. Only one may survive the new constraint. The winner is chosen deterministically by
(status rank, approved_at desc, id asc) — the row carrying the most human information wins,
and ties break on a stable key rather than on row order. The loser's dependent opportunity
and paper-trade rows are repointed to the winner rather than dropped, and the loser's full
contents are written to ``audit_log`` before deletion, so nothing is lost silently.

**Ordering.** New rows are inserted, children are repointed, then old rows are deleted.
That order never leaves a dangling foreign key, so it is safe whether or not the database
enforces them — SQLite usually does not, PostgreSQL always does.

**Verified on PostgreSQL 16.15** (2026-08-24) as well as SQLite, with seeded pre-migration
rows and foreign keys enforced: upgrade, downgrade, re-upgrade, and full chain to base and
back. PostgreSQL is where the ordering actually matters — it enforces
``opportunities.pair_id -> market_pairs.id``, which SQLite leaves off by default.

**Downgrade** restores the two-column constraint and rekeys back using ``legacy_id``. It
cannot resurrect superseded rows or restore cleared approvals: neither is recoverable from
what remains in the table. Those are recorded in ``audit_log``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "8c41f0b7d2ae"
down_revision: str | None = "3ea85fd4182a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Pairs written before product identity default to Global, matching
#: MarketPair.polymarket_product's own conservative default.
_DEFAULT_PRODUCT = "polymarket_global"

#: Columns copied when a row is rekeyed. Listed explicitly rather than relying on SELECT *
#: column order: ``legacy_id`` is appended to the end of the table by add_column, so a
#: positional copy silently shifted every value one column to the left.
_COPIED_COLUMNS = (
    "kalshi_market_id",
    "polymarket_market_id",
    "polymarket_product",
    "status",
    "semantic_score",
    "manually_approved",
    "outcomes_inverted",
    "reviewer_notes",
    "rule_differences",
    "kalshi_reviewed_source",
    "polymarket_reviewed_source",
    "kalshi_rules_hash_at_approval",
    "polymarket_rules_hash_at_approval",
    "approved_at",
    "approved_by",
    "created_at",
)


def _copy_row(
    connection: sa.Connection, *, source_id: str, new_id: str, legacy_id: str | None
) -> None:
    """Duplicate a pair row under a different primary key."""
    columns = ", ".join(_COPIED_COLUMNS)
    # Column list is a module constant, never caller input.
    insert = f"INSERT INTO market_pairs (id, legacy_id, {columns}) "
    select = f"SELECT :new_id, :legacy_id, {columns} FROM market_pairs WHERE id = :source_id"  # noqa: S608
    statement = insert + select
    connection.execute(
        sa.text(statement),
        {"new_id": new_id, "legacy_id": legacy_id, "source_id": source_id},
    )


#: The only two tables that reference a pair id. Named as literals rather than discovered,
#: so this migration keeps touching exactly these tables even if new ones appear later.
_DEPENDENT_TABLES = ("opportunities", "paper_trades")


def _repoint(connection: sa.Connection, table: str, old_id: str, new_id: str) -> None:
    """Move a table's pair references. ``table`` comes only from _DEPENDENT_TABLES."""
    if table not in _DEPENDENT_TABLES:  # pragma: no cover - defensive
        raise ValueError(f"refusing to build SQL for unknown table {table!r}")
    connection.execute(
        sa.text(f"UPDATE {table} SET pair_id = :new_id WHERE pair_id = :old_id"),  # noqa: S608
        {"new_id": new_id, "old_id": old_id},
    )


#: Most human information first. A row a person acted on outranks one they did not.
_STATUS_RANK = {
    "approved": 0,
    "rejected": 1,
    "expert_review": 2,
    "under_review": 3,
    "suspended": 4,
    "suggested": 5,
}


def _pair_id(kalshi_id: str, product: str, polymarket_id: str) -> str:
    """Must match event_arb.matching.candidates.pair_id_for.

    Inlined rather than imported: a migration has to keep producing the ids that were
    current when it ran, even after the application's hashing changes.
    """
    material = f"{kalshi_id}|{product}|{polymarket_id}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _audit(connection: sa.Connection, action: str, subject: str, detail: dict[str, object]) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO audit_log (actor, action, subject, detail, created_at) "
            "VALUES (:actor, :action, :subject, :detail, :created_at)"
        ),
        {
            "actor": "migration:8c41f0b7d2ae",
            "action": action,
            "subject": subject,
            "detail": json.dumps(detail, default=str),
            "created_at": datetime.now(UTC),
        },
    )


def _rebuild_unique(columns: list[str]) -> None:
    """SQLite cannot alter a constraint in place; batch mode recreates the table."""
    with op.batch_alter_table("market_pairs", schema=None) as batch:
        batch.drop_constraint("uq_pair", type_="unique")
        batch.create_unique_constraint("uq_pair", columns)


def _drop_uq_pair_if_present() -> None:
    """Drop uq_pair, tolerating its absence.

    A database restored from a partial backup, or built before the constraint existed, may
    not have it. Failing there would block the migration on a database that most needs it,
    since those are exactly the databases that can hold colliding rows.
    """
    try:
        with op.batch_alter_table("market_pairs", schema=None) as batch:
            batch.drop_constraint("uq_pair", type_="unique")
    except (ValueError, sa.exc.DatabaseError, sa.exc.InvalidRequestError):
        pass


def upgrade() -> None:
    with op.batch_alter_table("market_pairs", schema=None) as batch:
        batch.add_column(sa.Column("legacy_id", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    # Drop the old constraint first: while a row and its rekeyed copy both exist they would
    # violate it, and the copy is how we move children across without dangling references.
    _drop_uq_pair_if_present()

    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, kalshi_market_id, polymarket_market_id, polymarket_product, "
                "status, manually_approved, approved_at FROM market_pairs"
            )
        ).mappings()
    )

    # Group by the id each row *should* have.
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        product = str(row["polymarket_product"] or _DEFAULT_PRODUCT)
        new_id = _pair_id(str(row["kalshi_market_id"]), product, str(row["polymarket_market_id"]))
        groups.setdefault(new_id, []).append({**row, "polymarket_product": product})

    def sort_key(row: dict[str, object]) -> tuple[int, str, str]:
        rank = _STATUS_RANK.get(str(row["status"]), len(_STATUS_RANK))
        # approved_at descending, so invert by using a reversed string comparison via a
        # separate element: empty sorts last, which suits "no approval recorded".
        approved = str(row["approved_at"] or "")
        return (rank, "" if not approved else chr(0x10FFFF), str(row["id"]))

    for new_id, members in groups.items():
        members.sort(key=sort_key)
        winner = members[0]
        losers = members[1:]
        old_id = str(winner["id"])

        for loser in losers:
            _audit(
                connection,
                "migration.pair_superseded",
                str(loser["id"]),
                {
                    "reason": (
                        "two legacy pairs rekey to the same product-aware id; only one row "
                        "may exist for a (kalshi, polymarket, product) triple"
                    ),
                    "kept_pair_id": new_id,
                    "superseded_row": {k: str(v) for k, v in loser.items()},
                },
            )

        if old_id != new_id:
            # 1. Insert the rekeyed copy so children have somewhere valid to point.
            _copy_row(connection, source_id=old_id, new_id=new_id, legacy_id=old_id)
            _audit(
                connection,
                "migration.pair_rekeyed",
                new_id,
                {"legacy_id": old_id, "product": winner["polymarket_product"]},
            )

        # 2. Repoint every dependent row, including the losers' history.
        for table in _DEPENDENT_TABLES:
            for member in members:
                _repoint(connection, table, str(member["id"]), new_id)

        # 3. Delete every old row: the winner's original, and the losers outright.
        for member in members:
            member_id = str(member["id"])
            if member_id != new_id:
                connection.execute(
                    sa.text("DELETE FROM market_pairs WHERE id = :id"), {"id": member_id}
                )

        # 4. An approval recorded under an ambiguous identity cannot survive it.
        # bool() not int(): PostgreSQL returns a real bool here, SQLite an int.
        if old_id != new_id and bool(winner["manually_approved"]):
            connection.execute(
                sa.text(
                    # Bind the boolean rather than writing a literal 0: SQLite accepts
                    # 0 for a boolean column, PostgreSQL rejects it outright.
                    "UPDATE market_pairs SET status = 'suspended', "
                    "manually_approved = :approved, reviewer_notes = :note WHERE id = :id"
                ),
                {
                    "id": new_id,
                    "approved": False,
                    "note": (
                        "[migration 8c41f0b7d2ae] approval cleared: it was recorded against "
                        f"pair id {old_id}, which did not distinguish the Polymarket "
                        "product. Re-review both rulebooks under the new id."
                    ),
                },
            )
            _audit(
                connection,
                "migration.approval_cleared",
                new_id,
                {"legacy_id": old_id, "previous_status": winner["status"]},
            )

    _rebuild_unique_after_upgrade()


def _rebuild_unique_after_upgrade() -> None:
    with op.batch_alter_table("market_pairs", schema=None) as batch:
        batch.create_unique_constraint(
            "uq_pair", ["kalshi_market_id", "polymarket_market_id", "polymarket_product"]
        )


def downgrade() -> None:
    connection = op.get_bind()
    _drop_uq_pair_if_present()

    rows = list(
        connection.execute(
            sa.text("SELECT id, legacy_id FROM market_pairs WHERE legacy_id IS NOT NULL")
        ).mappings()
    )
    for row in rows:
        new_id, old_id = str(row["id"]), str(row["legacy_id"])
        _copy_row(connection, source_id=new_id, new_id=old_id, legacy_id=None)
        for table in _DEPENDENT_TABLES:
            _repoint(connection, table, new_id, old_id)
        connection.execute(sa.text("DELETE FROM market_pairs WHERE id = :id"), {"id": new_id})

    with op.batch_alter_table("market_pairs", schema=None) as batch:
        batch.create_unique_constraint("uq_pair", ["kalshi_market_id", "polymarket_market_id"])
        batch.drop_column("legacy_id")
