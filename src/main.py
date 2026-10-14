"""Application factory and lifespan. Startup never imports data, places calls or contacts any
provider; it only opens the database engines and creates (unused-until-needed) HTTP clients."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.billing.router import router as installments_router
from src.calls.router import router as calls_router
from src.common.exceptions import AppError
from src.common.logging import CorrelationIdMiddleware, configure_logging, correlation_id_var
from src.config import Settings, get_settings
from src.customers.router import router as customers_router
from src.database import create_engine, create_readonly_engine, create_session_factory
from src.database_query.router import router as database_router
from src.database_query.schema_registry import SchemaRegistry
from src.llm.client import FireworksClient, LLMClient, create_http_client
from src.recovery.router import router as recovery_router
from src.simulation.processor import EventProcessor
from src.simulation.router import router as simulation_router
from src.telephony.base import TelephonyProvider
from src.telephony.factory import create_provider
from src.telephony.router import router as twilio_router
from src.tools.builtin import build_default_registry
from src.voice.conversation import VoiceConversationService

logger = logging.getLogger(__name__)


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": details},
            "correlation_id": correlation_id_var.get(),
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo locations and messages only, never the submitted input.
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _error(422, "request_validation_error", "Request validation failed", details)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"exc_type": type(exc).__name__})
        return _error(500, "internal_error", "Internal server error")


def create_app(
    settings: Settings | None = None,
    *,
    llm_client: LLMClient | None = None,
    telephony: TelephonyProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging(settings.log_level)
        engine = create_engine(settings)
        readonly_engine = create_readonly_engine(settings) if settings.sql_query_allowed else None
        http_client = create_http_client(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.processor = EventProcessor(app.state.session_factory, settings)
        # Fails fast if Twilio is selected but not fully configured; mock never dials.
        app.state.telephony = telephony or create_provider(settings)
        app.state.readonly_engine = readonly_engine
        app.state.llm_client = llm_client or FireworksClient(http_client, settings)
        app.state.schema_registry = SchemaRegistry()
        app.state.tool_registry = build_default_registry()
        app.state.voice = VoiceConversationService(
            app.state.session_factory, app.state.llm_client, app.state.tool_registry, settings
        )
        logger.info(
            "startup",
            extra={
                "app_env": settings.app_env,
                "sql_query_enabled": settings.sql_query_allowed,
                "llm_configured": settings.llm_configured,
            },
        )
        try:
            yield
        finally:
            await app.state.voice.aclose()
            await http_client.aclose()
            if readonly_engine is not None:
                await readonly_engine.dispose()
            await engine.dispose()

    app = FastAPI(
        title="Autopay Recovery Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
        description="Synthetic-data backend for the autopay recovery voice agent. "
        "All /api/v1 routes except /health require the X-API-Key operator header.",
    )
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    api = APIRouter(prefix="/api/v1")

    @api.get("/health", tags=["health"])
    async def health(request: Request) -> JSONResponse:
        """Liveness + database check. Optional providers are reported, never required."""
        try:
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
            database = "ok"
        except Exception:
            logger.exception("health check database failure")
            database = "unavailable"
        cfg: Settings = request.app.state.settings
        body = {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "providers": {
                "llm": "configured" if cfg.llm_configured else "not_configured",
                "telephony": request.app.state.telephony.name,
                "live_calls_enabled": cfg.telephony_provider == "twilio" and cfg.twilio_ready,
            },
        }
        return JSONResponse(status_code=200 if database == "ok" else 503, content=body)

    api.include_router(customers_router)
    api.include_router(database_router)
    api.include_router(simulation_router)
    api.include_router(installments_router)
    api.include_router(recovery_router)
    api.include_router(twilio_router)
    api.include_router(calls_router)
    app.include_router(api)
    return app


app = create_app()
