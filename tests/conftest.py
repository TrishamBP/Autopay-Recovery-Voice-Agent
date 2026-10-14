"""Shared fixtures: an isolated file-backed SQLite database per test, settings with no external
credentials, a fake LLM, and an in-process HTTP client (lifespan entered explicitly because
httpx.ASGITransport does not run lifespan events)."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.config import Settings
from src.database import create_engine, create_session_factory
from src.importers.excel_importer import ImportOptions, run_import
from src.llm.client import ChatMessage, LLMError
from src.main import create_app
from src.models_registry import Base
from tests.factories import write_workbook

OPERATOR_KEY = "test-operator-key"


class FakeLLM:
    """Scripted LLM. Each `complete` call pops the next response (str or exception)."""

    def __init__(self, *responses: str | LLMError) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    async def complete(
        self, messages: list[ChatMessage], *, max_tokens: int, temperature: float = 0.0
    ) -> str:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        response = self.responses.pop(0)
        if isinstance(response, LLMError):
            raise response
        return response


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        "operator_api_key": OPERATOR_KEY,
        "sql_query_enabled": True,
        "fireworks_api_key": None,
        "fireworks_model": None,
        "workbook_path": tmp_path / "fixture.xlsx",
        "test_destination_allowlist": "+15005550006",
        "allow_synthetic_consent_for_test_destinations": True,
        "telephony_provider": "mock",
        "simulation_dev_endpoints_enabled": True,
    }
    return Settings(_env_file=None, **(values | overrides))  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return write_workbook(tmp_path / "fixture.xlsx")


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession], workbook: Path) -> None:
    report = await run_import(session_factory, workbook, ImportOptions())
    assert report.outcome == "imported", report.to_dict()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def client(
    settings: Settings, engine: AsyncEngine, seeded: None, fake_llm: FakeLLM
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, llm_client=fake_llm)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": OPERATOR_KEY}
        ) as http:
            yield http
