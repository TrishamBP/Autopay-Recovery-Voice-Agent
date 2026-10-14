"""Async engine, session factory, declarative base and SQLite connection policy.

SQLite policy (applied on every new DBAPI connection):
- `foreign_keys=ON`: SQLite does not enforce FKs unless asked, per connection.
- `busy_timeout`: a writer waits up to `SQLITE_BUSY_TIMEOUT_MS` for the single write lock
  instead of failing immediately with "database is locked".
- `journal_mode=WAL` (file databases, when `SQLITE_WAL=true`): readers don't block the writer and
  vice versa. Still only ONE writer at a time; WAL needs a local filesystem (no network shares)
  and adds `-wal`/`-shm` side files.
- `synchronous=NORMAL` with WAL: durable across application crashes; a power loss can drop the
  most recent commits. Acceptable for a local prototype.

The read-only engine (SQL query endpoint) opens the file with `mode=ro` and sets
`query_only=ON`, so writes fail at the SQLite level even if application validation were bypassed.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import MetaData, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def is_sqlite(url: str | URL) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def sqlite_file_path(url: str | URL) -> Path | None:
    """Filesystem path of a file-backed SQLite URL, or None (memory DB / other backends)."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database:
        return None
    database = parsed.database
    if database == ":memory:" or database.startswith("file::memory:"):
        return None
    return Path(database.removeprefix("file:"))


def ensure_sqlite_directory(url: str | URL) -> None:
    path = sqlite_file_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def _install_sqlite_pragmas(engine: AsyncEngine, *, busy_timeout_ms: int, wal: bool) -> None:
    file_backed = sqlite_file_path(engine.url) is not None

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if wal and file_backed:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """The application's single read/write engine. Create once (lifespan), dispose on shutdown."""
    ensure_sqlite_directory(settings.database_url)
    engine = create_async_engine(settings.database_url, echo=settings.database_echo)
    if is_sqlite(settings.database_url):
        _install_sqlite_pragmas(
            engine, busy_timeout_ms=settings.sqlite_busy_timeout_ms, wal=settings.sqlite_wal
        )
    return engine


def create_readonly_engine(settings: Settings) -> AsyncEngine:
    """Engine for untrusted read-only queries: `mode=ro` URI + `PRAGMA query_only=ON`."""
    path = sqlite_file_path(settings.database_url)
    if path is None:
        raise ValueError("The read-only query engine requires a file-backed SQLite database")
    url = f"sqlite+aiosqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true"
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services open explicit transactions with `session.begin()`;
    anything left uncommitted is rolled back when the session closes."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session
