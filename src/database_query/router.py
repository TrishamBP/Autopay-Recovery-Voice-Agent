"""`/database/*` operator endpoints. The query endpoint exists only when SQL_QUERY_ENABLED=true
and APP_ENV is development/test; otherwise it answers 404 as if absent."""

from fastapi import APIRouter, Request

from src.common.dependencies import OperatorDep, SessionDep, SettingsDep
from src.common.exceptions import NotFoundError, ServiceUnavailableError
from src.common.schemas import ErrorResponse
from src.database_query import service
from src.database_query.schemas import (
    DatabaseQueryRequest,
    DatabaseQueryResponse,
    DatabaseSummaryResponse,
)

router = APIRouter(
    prefix="/database",
    tags=["database"],
    dependencies=[OperatorDep],
    responses={401: {"model": ErrorResponse}},
)


@router.get("/summary", response_model=DatabaseSummaryResponse)
async def database_summary(session: SessionDep) -> DatabaseSummaryResponse:
    return await service.database_summary(session)


@router.post(
    "/query",
    response_model=DatabaseQueryResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Endpoint disabled"},
        422: {"model": ErrorResponse, "description": "Question or generated SQL rejected"},
        502: {"model": ErrorResponse, "description": "LLM failure"},
        503: {"model": ErrorResponse, "description": "LLM not configured"},
        504: {"model": ErrorResponse, "description": "LLM timeout"},
    },
)
async def database_query(
    body: DatabaseQueryRequest, request: Request, settings: SettingsDep
) -> DatabaseQueryResponse:
    if not settings.sql_query_allowed:
        raise NotFoundError("Not Found")
    engine = request.app.state.readonly_engine
    if engine is None:
        raise ServiceUnavailableError("The read-only query engine is unavailable")
    return await service.run_query(
        body,
        llm=request.app.state.llm_client,
        engine=engine,
        registry=request.app.state.schema_registry,
        settings=settings,
    )
