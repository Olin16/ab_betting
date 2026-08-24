"""Upgrade/downgrade of the pair-identity migration against seeded legacy rows.

Every test builds its own temporary database file. The project database is never opened.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

REVISION = "8c41f0b7d2ae"
PREVIOUS = "3ea85fd4182a"


def legacy_pair_id(kalshi_id: str, polymarket_id: str) -> str:
    """The pre-migration scheme: no product component."""
    return hashlib.sha256(f"{kalshi_id}|{polymarket_id}".encode()).hexdigest()[:32]


def product_pair_id(kalshi_id: str, product: str, polymarket_id: str) -> str:
    return hashlib.sha256(f"{kalshi_id}|{product}|{polymarket_id}".encode()).hexdigest()[:32]


def alembic(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, "EVENT_ARB_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        check=True,
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Path]:
    """A brand-new file per test. Never data/event_arb.db."""
    path = tmp_path / "migration.db"
    alembic(path, "upgrade", PREVIOUS)
    yield path


def seed_pair(
    database: Path,
    *,
    pair_id: str,
    kalshi_id: str = "KXFED-26SEP",
    polymarket_id: str = "fed-sep",
    product: str = "polymarket_global",
    status: str = "approved",
    approved: int = 1,
    approved_at: str | None = "2026-08-01T00:00:00+00:00",
) -> None:
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO market_pairs (id, kalshi_market_id, polymarket_market_id, "
        "polymarket_product, status, semantic_score, manually_approved, outcomes_inverted, "
        "reviewer_notes, rule_differences, kalshi_rules_hash_at_approval, "
        "polymarket_rules_hash_at_approval, approved_at, approved_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pair_id,
            kalshi_id,
            polymarket_id,
            product,
            status,
            "0.95",
            approved,
            0,
            f"human note for {pair_id[:8]}",
            json.dumps({"differences": []}),
            "hash-k",
            "hash-p",
            approved_at,
            "human@example.com" if approved else "",
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()
    connection.close()


def seed_children(database: Path, pair_id: str, *, opportunities: int, trades: int) -> None:
    connection = sqlite3.connect(database)
    for _ in range(opportunities):
        connection.execute(
            "INSERT INTO opportunities (pair_id, direction, label, executable_quantity, "
            "gross_acquisition_cost, kalshi_fees, polymarket_fees, slippage_allowance, "
            "operational_cost_allowance, usdc_conversion_allowance, expected_payout, "
            "net_profit, return_on_capital, risk_flags, snapshot, detected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pair_id,
                "a_kalshi_yes_poly_no",
                "confirmed",
                "10",
                "9",
                "0.05",
                "0.05",
                "0.01",
                "0.01",
                "0",
                "10",
                "0.9",
                "10",
                json.dumps([]),
                json.dumps({}),
                datetime.now(UTC).isoformat(),
            ),
        )
    for _ in range(trades):
        connection.execute(
            "INSERT INTO paper_trades (pair_id, direction, status, opportunity_snapshot, "
            "orders, realized_pnl, unhedged_quantity, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                pair_id,
                "a_kalshi_yes_poly_no",
                "both_legs_filled",
                json.dumps({}),
                json.dumps([]),
                "0.9",
                "0",
                datetime.now(UTC).isoformat(),
            ),
        )
    connection.commit()
    connection.close()


def drop_legacy_unique(database: Path) -> None:
    """Rebuild market_pairs without its uq_pair constraint.

    The pre-migration constraint covers (kalshi_market_id, polymarket_market_id), so two
    rows that rekey to the same product-aware id cannot normally coexist — the collision
    branch is defensive. It still has to be correct: a database restored from a partial
    backup, or one built before the constraint existed, can hold such rows, and the
    migration must not lose data when it meets them.

    SQLite stores this constraint inline in CREATE TABLE, backed by an autoindex that
    DROP INDEX cannot remove, so the table is recreated from its own DDL with the
    constraint clause stripped out.
    """
    connection = sqlite3.connect(database)
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_pairs'"
    ).fetchone()[0]
    stripped = re.sub(r",?\s*CONSTRAINT uq_pair UNIQUE \([^)]*\)", "", ddl)
    assert stripped != ddl, "uq_pair clause not found; the DDL shape changed"

    connection.executescript(
        "PRAGMA foreign_keys=OFF;\n"
        "ALTER TABLE market_pairs RENAME TO market_pairs_old;\n"
        f"{stripped};\n"
        "INSERT INTO market_pairs SELECT * FROM market_pairs_old;\n"
        "DROP TABLE market_pairs_old;\n"
    )
    connection.commit()
    connection.close()


def rows(database: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return list(connection.execute(sql))
    finally:
        connection.close()


# ------------------------------------------------------------------------------ rekeying


def test_every_legacy_pair_is_rekeyed_not_only_approved_ones(database: Path) -> None:
    """Unapproved legacy rows collide with newly generated ids too."""
    for status, approved, poly in [
        ("approved", 1, "fed-a"),
        ("suggested", 0, "fed-b"),
        ("rejected", 0, "fed-c"),
    ]:
        seed_pair(
            database,
            pair_id=legacy_pair_id("KXFED-26SEP", poly),
            polymarket_id=poly,
            status=status,
            approved=approved,
        )

    alembic(database, "upgrade", REVISION)

    for poly in ("fed-a", "fed-b", "fed-c"):
        expected = product_pair_id("KXFED-26SEP", "polymarket_global", poly)
        found = rows(database, f"SELECT id, legacy_id FROM market_pairs WHERE id = '{expected}'")
        assert found, f"{poly} was not rekeyed"
        assert found[0][1] == legacy_pair_id("KXFED-26SEP", poly)


def test_dependent_opportunity_and_trade_rows_follow_the_new_id(database: Path) -> None:
    old = legacy_pair_id("KXFED-26SEP", "fed-sep")
    seed_pair(database, pair_id=old)
    seed_children(database, old, opportunities=3, trades=2)

    alembic(database, "upgrade", REVISION)

    new = product_pair_id("KXFED-26SEP", "polymarket_global", "fed-sep")
    assert rows(database, f"SELECT count(*) FROM opportunities WHERE pair_id='{new}'")[0][0] == 3
    assert rows(database, f"SELECT count(*) FROM paper_trades WHERE pair_id='{new}'")[0][0] == 2
    assert rows(database, f"SELECT count(*) FROM opportunities WHERE pair_id='{old}'")[0][0] == 0


def test_an_approval_under_the_old_identity_is_cleared(database: Path) -> None:
    """The approval described a pair identity that did not name a product."""
    seed_pair(database, pair_id=legacy_pair_id("KXFED-26SEP", "fed-sep"), status="approved")

    alembic(database, "upgrade", REVISION)

    new = product_pair_id("KXFED-26SEP", "polymarket_global", "fed-sep")
    status, approved, notes = rows(
        database,
        f"SELECT status, manually_approved, reviewer_notes FROM market_pairs WHERE id='{new}'",
    )[0]
    assert status == "suspended"
    assert approved == 0
    assert "Re-review both rulebooks" in str(notes)


def test_an_unapproved_pair_keeps_its_status(database: Path) -> None:
    """Only the approval flag is destroyed; other dispositions survive rekeying."""
    seed_pair(
        database,
        pair_id=legacy_pair_id("KXFED-26SEP", "fed-sep"),
        status="rejected",
        approved=0,
        approved_at=None,
    )

    alembic(database, "upgrade", REVISION)

    new = product_pair_id("KXFED-26SEP", "polymarket_global", "fed-sep")
    status, notes = rows(
        database, f"SELECT status, reviewer_notes FROM market_pairs WHERE id='{new}'"
    )[0]
    assert status == "rejected"
    assert "human note" in str(notes)


# ---------------------------------------------------------------------------- uniqueness


def test_global_and_us_pairs_with_identical_venue_ids_can_coexist(database: Path) -> None:
    """The whole point: uq_pair previously made these one row."""
    alembic(database, "upgrade", REVISION)

    seed_pair(
        database,
        pair_id=product_pair_id("K1", "polymarket_global", "SAME-ID"),
        kalshi_id="K1",
        polymarket_id="SAME-ID",
        product="polymarket_global",
    )
    seed_pair(
        database,
        pair_id=product_pair_id("K1", "polymarket_us", "SAME-ID"),
        kalshi_id="K1",
        polymarket_id="SAME-ID",
        product="polymarket_us",
    )

    found = rows(
        database, "SELECT polymarket_product FROM market_pairs ORDER BY polymarket_product"
    )
    assert [r[0] for r in found] == ["polymarket_global", "polymarket_us"]


def test_the_same_legs_and_product_are_still_rejected(database: Path) -> None:
    """Uniqueness is widened, not removed."""
    alembic(database, "upgrade", REVISION)
    seed_pair(database, pair_id="a" * 32, kalshi_id="K1", polymarket_id="P1")
    with pytest.raises(sqlite3.IntegrityError):
        seed_pair(database, pair_id="b" * 32, kalshi_id="K1", polymarket_id="P1")


# ---------------------------------------------------------------------------- collisions


def test_colliding_legacy_pairs_keep_the_most_human_row_and_lose_nothing(
    database: Path,
) -> None:
    """Two rows, same legs and product, different ids: only one may survive."""
    weak, strong = "1" * 32, "2" * 32
    drop_legacy_unique(database)
    seed_pair(database, pair_id=weak, status="suggested", approved=0, approved_at=None)
    seed_pair(database, pair_id=strong, status="approved", approved=1)
    seed_children(database, weak, opportunities=1, trades=1)
    seed_children(database, strong, opportunities=2, trades=0)

    alembic(database, "upgrade", REVISION)

    new = product_pair_id("KXFED-26SEP", "polymarket_global", "fed-sep")
    assert rows(database, "SELECT count(*) FROM market_pairs")[0][0] == 1
    assert rows(database, f"SELECT legacy_id FROM market_pairs WHERE id='{new}'")[0][0] == strong

    # The loser's history is repointed, not deleted.
    assert rows(database, f"SELECT count(*) FROM opportunities WHERE pair_id='{new}'")[0][0] == 3
    assert rows(database, f"SELECT count(*) FROM paper_trades WHERE pair_id='{new}'")[0][0] == 1

    # And the loser's own contents survive in the audit trail.
    audit = rows(
        database, "SELECT subject, detail FROM audit_log WHERE action='migration.pair_superseded'"
    )
    assert len(audit) == 1
    assert audit[0][0] == weak
    assert "superseded_row" in str(audit[0][1])


def test_collision_resolution_does_not_depend_on_insertion_order(
    database: Path, tmp_path: Path
) -> None:
    """The winner must be the same whichever row was written first."""
    other = tmp_path / "reversed.db"
    alembic(other, "upgrade", PREVIOUS)
    drop_legacy_unique(database)
    drop_legacy_unique(other)

    seed_pair(database, pair_id="1" * 32, status="suggested", approved=0, approved_at=None)
    seed_pair(database, pair_id="2" * 32, status="approved", approved=1)
    seed_pair(other, pair_id="2" * 32, status="approved", approved=1)
    seed_pair(other, pair_id="1" * 32, status="suggested", approved=0, approved_at=None)

    alembic(database, "upgrade", REVISION)
    alembic(other, "upgrade", REVISION)

    assert rows(database, "SELECT legacy_id FROM market_pairs") == rows(
        other, "SELECT legacy_id FROM market_pairs"
    )


# ------------------------------------------------------------------------------- reverse


def test_upgrade_downgrade_upgrade_is_stable(database: Path) -> None:
    old = legacy_pair_id("KXFED-26SEP", "fed-sep")
    seed_pair(database, pair_id=old, status="rejected", approved=0, approved_at=None)
    seed_children(database, old, opportunities=2, trades=1)

    alembic(database, "upgrade", REVISION)
    alembic(database, "downgrade", PREVIOUS)

    # Back to the legacy id, with children following.
    assert rows(database, f"SELECT count(*) FROM market_pairs WHERE id='{old}'")[0][0] == 1
    assert rows(database, f"SELECT count(*) FROM opportunities WHERE pair_id='{old}'")[0][0] == 2
    assert rows(database, f"SELECT count(*) FROM paper_trades WHERE pair_id='{old}'")[0][0] == 1

    alembic(database, "upgrade", REVISION)
    new = product_pair_id("KXFED-26SEP", "polymarket_global", "fed-sep")
    assert rows(database, f"SELECT count(*) FROM market_pairs WHERE id='{new}'")[0][0] == 1


def test_downgrade_restores_the_two_column_constraint(database: Path) -> None:
    """Global and US pairs must stop coexisting once the old constraint is back."""
    alembic(database, "upgrade", REVISION)
    alembic(database, "downgrade", PREVIOUS)

    seed_pair(
        database, pair_id="a" * 32, kalshi_id="K1", polymarket_id="P1", product="polymarket_global"
    )
    with pytest.raises(sqlite3.IntegrityError):
        seed_pair(
            database, pair_id="b" * 32, kalshi_id="K1", polymarket_id="P1", product="polymarket_us"
        )


def test_an_empty_database_migrates_cleanly(database: Path) -> None:
    alembic(database, "upgrade", REVISION)
    assert rows(database, "SELECT count(*) FROM market_pairs")[0][0] == 0
    alembic(database, "downgrade", PREVIOUS)
