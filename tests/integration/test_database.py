from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
from src.config import Settings
from src.customer_notes.models import CustomerNote
from src.customers.models import Customer
from src.database import create_engine
from src.models_registry import Base

EXPECTED_TABLES = {
    "customer",
    "payment_history",
    "customer_note",
    "call_session",
    "conversation_turn",
    "tool_execution",
    "callback_request",
    "payment_link",
    "audit_log",
    "call_scenario",
}


async def test_all_tables_and_foreign_keys_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("payment_history"))
    assert tables >= EXPECTED_TABLES
    assert fks[0]["referred_table"] == "customer"
    assert fks[0]["options"].get("ondelete") == "RESTRICT"


async def test_sqlite_pragmas_are_applied(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"


async def test_foreign_keys_are_enforced(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async with session_factory() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(text("DELETE FROM customer WHERE customer_id = 'CUS-1001'"))


async def test_transaction_rolls_back_on_failure(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async with session_factory() as s:
        with pytest.raises(RuntimeError):
            async with s.begin():
                customer = await s.get(Customer, "CUS-1001")
                assert customer is not None
                customer.full_name = "Changed"
                await s.flush()
                raise RuntimeError("boom")
    async with session_factory() as s:
        customer = await s.get(Customer, "CUS-1001")
        assert customer is not None and customer.full_name == "Aarav Mehta"


async def test_naive_datetimes_are_rejected(
    session_factory: async_sessionmaker[AsyncSession], seeded: None
) -> None:
    async with session_factory() as s:
        with pytest.raises(StatementError, match="timezone-aware"):
            async with s.begin():
                note = await s.get(CustomerNote, "NOTE-0001")
                assert note is not None
                note.updated_at = datetime(2026, 10, 1, 12, 0)  # naive: must be rejected
                await s.flush()


async def test_engine_dispose_closes_connections(settings: Settings) -> None:
    engine = create_engine(settings)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    assert engine.pool.checkedin() == 1
    await engine.dispose()
    assert engine.pool.checkedin() == 0


def test_alembic_migration_matches_models(tmp_path: Path) -> None:
    """The migration (not create_all) is the production schema path; it must match the models."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    command.check(config)  # raises if autogenerate would emit any operation

    import sqlite3

    conn = sqlite3.connect(tmp_path / "migrated.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(Base.metadata.tables) <= tables
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    command.downgrade(config, "base")
