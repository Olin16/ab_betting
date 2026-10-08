"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from event_arb.persistence.models import Base

__all__ = ["Database", "build_engine"]


def build_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create the async engine, ensuring the SQLite parent directory exists.

    In-memory SQLite additionally gets :class:`StaticPool`. Each connection to
    ``:memory:`` opens its **own** empty database, so with the default pool a second
    concurrent connection sees no tables at all — the schema was created on the first one.
    Serial callers never noticed; the trial's concurrent loops do, and the symptom is a
    bewildering "no such table" against a database that was demonstrably created. Pinning
    one connection makes ``:memory:`` mean one database, which is what every caller
    already assumed.
    """
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        path_part = database_url.split("///", 1)[-1]
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    if database_url.startswith("sqlite") and ":memory:" in database_url:
        return create_async_engine(
            database_url,
            echo=echo,
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(database_url, echo=echo, future=True)


class Database:
    """Thin lifecycle wrapper around the engine and session factory."""

    def __init__(self, database_url: str, echo: bool = False) -> None:
        self._engine = build_engine(database_url, echo=echo)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> None:
        """Create tables directly.

        Used for tests and local bootstrapping. Production schema changes go through
        Alembic migrations.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                # BaseException, not Exception: a cancellation arriving mid-transaction
                # left the session un-rolled-back, and an aiosqlite connection returned
                # to the pool with an open transaction is invalid — which surfaced as a
                # later query finding an empty database rather than as an error here.
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()
