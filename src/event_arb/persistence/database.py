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

from event_arb.persistence.models import Base

__all__ = ["Database", "build_engine"]


def build_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create the async engine, ensuring the SQLite parent directory exists."""
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        path_part = database_url.split("///", 1)[-1]
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)
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
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()
