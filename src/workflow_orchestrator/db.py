"""Async SQLAlchemy engine/session plumbing.

NFR-5: SQLite runs in WAL mode so live log streaming readers do not contend
with writers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings
from .models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _apply_pragmas(dbapi_connection: object, _record: object) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")  # NFR-5
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def build_engine(settings: Settings) -> AsyncEngine:
    settings.ensure_dirs()
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = build_engine(settings)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialised; call init_engine() first")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on failure."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def apply_sqlite_pragmas(engine: AsyncEngine) -> str:
    """Open a connection so the pragma listener runs, and report the journal mode.

    WAL is a persistent property of the database file, but it is only set when
    something actually connects. Alembic's synchronous engine creates the file in
    ``delete`` mode, so startup has to establish WAL explicitly rather than
    assuming a request will come along and do it (NFR-5).
    """
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql("PRAGMA journal_mode")
        mode = str(result.scalar() or "unknown")
        if mode.lower() != "wal":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            result = await conn.exec_driver_sql("PRAGMA journal_mode")
            mode = str(result.scalar() or "unknown")
    return mode


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
