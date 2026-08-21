"""Async engine and session factory.

SQLAlchemy's async layer is what makes the eventual PostgreSQL move a
connection-string change rather than a rewrite (ARCHITECTURE.md §8), so
everything goes through `session_scope()` — no synchronous engine
anywhere.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import Settings
from bot.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _ensure_parent_dir(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def init_engine(settings: Settings) -> AsyncEngine:
    """Build the process-wide engine. Called once from main.py."""
    global _engine, _sessionmaker

    if _engine is not None:
        return _engine

    _ensure_parent_dir(settings.db_path)
    _engine = create_async_engine(settings.db_url, echo=False, future=True)

    # WAL lets the monitor write while the bot reads; foreign keys are
    # off by default in SQLite, and we rely on ON DELETE CASCADE.
    @event.listens_for(_engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() must be called before using the database")
    return _sessionmaker


async def create_schema() -> None:
    """Create missing tables. Real migrations belong in a migration tool."""
    if _engine is None:
        raise RuntimeError("init_engine() must be called first")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commits on success, rolls back on error."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def healthcheck() -> bool:
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
