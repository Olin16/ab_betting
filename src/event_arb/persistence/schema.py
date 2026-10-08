"""Schema revision the application expects, and how to check a database against it.

``Database.create_all()`` creates tables that do not exist. It does **not** alter tables
that do, so pointing a new build at an older database produced a process that started
cleanly and then failed on the first write to a column that was never added. For a
fixture demo that is a nuisance; for a live-data trial it is a run that has to be thrown
away, so the trial preflight refuses to start against a database whose revision is not
current.

:data:`SCHEMA_REVISION` is stated here rather than read from the Alembic scripts at
runtime, because the scripts are not part of the installed package. It cannot drift:
``tests/unit/test_schema_revision.py`` computes the real head from ``alembic/versions``
and asserts they match.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from event_arb.persistence.database import Database
from event_arb.persistence.models import Base

__all__ = [
    "SCHEMA_REVISION",
    "SchemaState",
    "ensure_schema",
    "inspect_schema",
]

#: Alembic head this build's ORM models correspond to.
SCHEMA_REVISION = "6b2f1c9a7d40"


#: "Empty" means *no* application table, not the absence of one chosen table.
#:
#: This was a single sentinel, ``paper_trades``, which every current schema has — but which
#: databases predating the migration that added it do not. Such a database was read as
#: empty, so ``ensure_schema`` created the missing tables beside the stale ones and stamped
#: the result at head: a database reported as current whose older tables were still the
#: shape the previous revision left them, which then failed on the first read of a column
#: the migration was supposed to have added. That is precisely the outcome this module
#: exists to prevent, so the test is now "does this hold anything we recognise".
def _application_tables() -> frozenset[str]:
    return declared_tables()


@dataclass(frozen=True)
class SchemaState:
    """What a database currently holds, and whether it is safe to run against."""

    exists: bool
    revision: str | None
    expected: str = SCHEMA_REVISION

    @property
    def is_current(self) -> bool:
        return self.exists and self.revision == self.expected

    @property
    def is_empty(self) -> bool:
        """No application tables at all: a fresh file, safe to create and stamp."""
        return not self.exists

    @property
    def problem(self) -> str | None:
        """A sentence naming the problem and the command that fixes it, or ``None``."""
        if self.is_current:
            return None
        if self.is_empty:
            return (
                "database has no schema yet; create it with 'alembic upgrade head' "
                f"(expected revision {self.expected})"
            )
        if self.revision is None:
            return (
                "database has tables but no alembic_version row, so its schema cannot be "
                "identified. Point EVENT_ARB_DATABASE_URL at a database created by "
                "'alembic upgrade head', or stamp this one only if you are certain which "
                "revision it matches."
            )
        return (
            f"database is at alembic revision {self.revision!r} but this build expects "
            f"{self.expected!r}. Run 'alembic upgrade head' against it. create_all() does "
            "not alter existing tables, so starting anyway would fail on the first write "
            "to a column the migration was meant to add."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "revision": self.revision,
            "expected": self.expected,
            "is_current": self.is_current,
            "problem": self.problem,
        }


def _read_state(connection: Connection) -> SchemaState:
    """Sync body run inside ``run_sync``: inspection needs a DBAPI connection."""
    tables = set(inspect(connection).get_table_names())
    if not (_application_tables() & tables):
        return SchemaState(exists=False, revision=None)
    if "alembic_version" not in tables:
        return SchemaState(exists=True, revision=None)
    row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    return SchemaState(exists=True, revision=str(row[0]) if row else None)


async def inspect_schema(database: Database) -> SchemaState:
    """Report the database's schema state without changing anything."""
    async with database.engine.connect() as connection:
        return await connection.run_sync(_read_state)


async def ensure_schema(database: Database, *, create_if_empty: bool = True) -> SchemaState:
    """Create and stamp an empty database; never silently migrate a populated one.

    Creating a fresh schema is safe and is what fixture runs and tests need. *Altering* an
    existing one is a decision with data on the other side of it, so this refuses and
    hands back a state whose :attr:`SchemaState.problem` names the migration command. The
    caller decides whether that is fatal — for a live-data trial the preflight says it is.
    """
    state = await inspect_schema(database)
    if state.is_current or not state.is_empty or not create_if_empty:
        return state

    await database.create_all()

    async def _stamp(connection: AsyncConnection) -> None:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await connection.execute(text("DELETE FROM alembic_version"))
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": SCHEMA_REVISION},
        )

    async with database.engine.begin() as connection:
        await _stamp(connection)
    return await inspect_schema(database)


def declared_tables() -> frozenset[str]:
    """Tables the ORM declares. Used by the migration tests to prove parity."""
    return frozenset(Base.metadata.tables)
