"""Orchestrates generate -> validate -> execute -> explain, and maps failures to safe errors."""

import hashlib
import logging

from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.common.exceptions import (
    AppError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationFailedError,
)
from src.common.redaction import sanitize_for_log
from src.config import Settings
from src.customers.models import Customer
from src.database_query.query_executor import (
    QueryExecutionError,
    QueryTimeoutError,
    execute_readonly,
)
from src.database_query.schema_registry import SchemaRegistry
from src.database_query.schemas import (
    DatabaseQueryRequest,
    DatabaseQueryResponse,
    DatabaseSummaryResponse,
    TableCount,
    ValidatedSql,
)
from src.database_query.sql_generator import (
    QuestionNotAnswerableError,
    explain_results,
    generate_sql,
)
from src.database_query.sql_validator import QueryRejectedError, validate_sql
from src.llm.client import (
    LLMClient,
    LLMError,
    LLMMalformedResponseError,
    LLMNotConfiguredError,
    LLMTimeoutError,
)
from src.models_registry import Base
from src.payment_history.models import PaymentHistory

logger = logging.getLogger(__name__)


class QueryRejected(AppError):
    status_code = 422
    code = "query_rejected"


class QueryTimedOut(AppError):
    status_code = 422
    code = "query_timeout"


class LLMTimedOut(AppError):
    status_code = 504
    code = "llm_timeout"


class LLMNotConfigured(ServiceUnavailableError):
    code = "llm_not_configured"


class LLMMalformedOutput(UpstreamError):
    code = "llm_malformed_output"


def _llm_error(exc: LLMError) -> AppError:
    if isinstance(exc, LLMNotConfiguredError):
        return LLMNotConfigured(
            "The LLM provider is not configured (set FIREWORKS_API_KEY and FIREWORKS_MODEL)."
        )
    if isinstance(exc, LLMTimeoutError):
        return LLMTimedOut("The LLM provider timed out.")
    if isinstance(exc, LLMMalformedResponseError):
        return LLMMalformedOutput("The LLM returned output that could not be used.")
    return UpstreamError("The LLM provider request failed.")


async def run_query(
    request: DatabaseQueryRequest,
    *,
    llm: LLMClient,
    engine: AsyncEngine,
    registry: SchemaRegistry,
    settings: Settings,
) -> DatabaseQueryResponse:
    if len(request.question) > settings.sql_max_question_chars:
        raise ValidationFailedError(
            f"question must be at most {settings.sql_max_question_chars} characters"
        )
    max_rows = min(request.limit, settings.sql_max_rows)
    log = {"question": sanitize_for_log(request.question), "max_rows": max_rows}
    try:
        candidate = await generate_sql(
            llm, registry, request.question, max_tokens=settings.llm_max_tokens
        )
        log["sql_sha256"] = hashlib.sha256(candidate.encode()).hexdigest()[:16]
        query = validate_sql(
            candidate, registry, max_rows=max_rows, max_chars=settings.sql_max_query_chars
        )
        result = await execute_readonly(
            engine,
            query,
            timeout_seconds=settings.sql_timeout_seconds,
            timestamp_columns=registry.timestamp_columns,
        )
    except QuestionNotAnswerableError as exc:
        logger.info("sql query not answerable", extra=log | {"outcome": "not_answerable"})
        raise QueryRejected(
            f"The question cannot be answered from the available data: {exc}"
        ) from exc
    except QueryRejectedError as exc:
        logger.warning(
            "sql query rejected", extra=log | {"outcome": "rejected", "reason": str(exc)}
        )
        raise QueryRejected(f"Generated SQL was rejected: {exc}") from exc
    except QueryTimeoutError as exc:
        logger.warning("sql query timed out", extra=log | {"outcome": "timeout"})
        raise QueryTimedOut("The query exceeded the execution time limit.") from exc
    except QueryExecutionError as exc:
        logger.warning("sql query failed", extra=log | {"outcome": "execution_error"})
        raise QueryRejected("The query could not be executed.") from exc
    except LLMError as exc:
        logger.warning("sql generation failed", extra=log | {"outcome": type(exc).__name__})
        raise _llm_error(exc) from exc

    explanation, status = None, "skipped"
    if request.explain and settings.sql_explain_max_rows > 0:
        try:
            explanation = await explain_results(
                llm,
                request.question,
                result,
                max_rows=settings.sql_explain_max_rows,
                max_tokens=settings.llm_max_tokens,
            )
            status = "ok"
        except LLMError:
            status = "unavailable"  # results are still valid without an explanation
    logger.info(
        "sql query executed",
        extra=log
        | {
            "outcome": "ok",
            "tables": list(query.tables),
            "row_count": len(result.rows),
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
        },
    )
    return DatabaseQueryResponse(
        question=request.question,
        query=ValidatedSql(
            validated_read_only_sql=query.sql,
            parameters=query.parameters,
            tables=list(query.tables),
        ),
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        explanation=explanation,
        explanation_status=status,  # type: ignore[arg-type]
    )


async def _group_counts(session: AsyncSession, column: object) -> dict[str, int]:
    rows = await session.execute(select(column, func.count()).group_by(column))  # type: ignore[call-overload]
    return {str(k.value if hasattr(k, "value") else k): n for k, n in rows.all()}


async def database_summary(session: AsyncSession) -> DatabaseSummaryResponse:
    counts = []
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        counts.append(
            TableCount(
                table=table.name,
                rows=await session.scalar(select(func.count()).select_from(table)) or 0,
            )
        )
    connection = await session.connection()
    has_alembic = await connection.run_sync(lambda c: inspect(c).has_table("alembic_version"))
    revision = (
        await session.scalar(text("SELECT version_num FROM alembic_version"))
        if has_alembic
        else None
    )
    return DatabaseSummaryResponse(
        tables=counts,
        customers_by_recovery_status=await _group_counts(session, Customer.recovery_status),
        customers_by_payment_status=await _group_counts(session, Customer.payment_status),
        payment_history_by_status=await _group_counts(session, PaymentHistory.status),
        do_not_contact_customers=await session.scalar(
            select(func.count()).select_from(Customer).where(Customer.do_not_contact.is_(True))
        )
        or 0,
        alembic_revision=revision,
    )
