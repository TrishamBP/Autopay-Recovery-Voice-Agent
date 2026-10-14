"""Executes an already-validated query on the READ-ONLY engine, bounded in rows and time.

Defence in depth: the connection was opened with `mode=ro` and `PRAGMA query_only=ON`, so even
a validator bug cannot write. Time is bounded by an SQLite progress handler that aborts the
statement at the deadline (SQLite raises "interrupted"), plus an outer asyncio timeout.
The SQL is passed to the driver as-is with a named-parameter dict; it is never formatted.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.database_query.sql_validator import ValidatedQuery

PROGRESS_HANDLER_OPCODES = 1000


class QueryTimeoutError(Exception):
    pass


class QueryExecutionError(Exception):
    pass


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[object]]
    truncated: bool
    duration_ms: float


def normalize_value(value: object, column: str, timestamp_columns: frozenset[str]) -> object:
    """JSON-safe values. Timestamps stored as naive-UTC text become ISO 8601 with 'Z'."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes):
        return "<binary omitted>"
    text = str(value)
    if column in timestamp_columns:
        try:
            return (
                datetime.fromisoformat(text).replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
            )
        except ValueError:
            return text
    return text


async def execute_readonly(
    engine: AsyncEngine,
    query: ValidatedQuery,
    *,
    timeout_seconds: float,
    timestamp_columns: frozenset[str],
) -> QueryResult:
    started = time.perf_counter()
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection  # aiosqlite.Connection
        deadline = time.monotonic() + timeout_seconds
        await driver.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_HANDLER_OPCODES
        )
        try:
            async with asyncio.timeout(timeout_seconds + 1.0):
                result = await conn.exec_driver_sql(query.sql, query.parameters)
                fetched = result.fetchmany(query.max_rows + 1)
                columns = list(result.keys())
        except TimeoutError as exc:
            await driver.interrupt()
            raise QueryTimeoutError("query exceeded the execution time limit") from exc
        except DBAPIError as exc:
            if "interrupted" in str(exc.orig).lower():
                raise QueryTimeoutError("query exceeded the execution time limit") from exc
            raise QueryExecutionError("query failed during execution") from exc
        finally:
            await driver.set_progress_handler(None, PROGRESS_HANDLER_OPCODES)
            await conn.rollback()
    rows = [
        [normalize_value(v, columns[i], timestamp_columns) for i, v in enumerate(row)]
        for row in fetched[: query.max_rows]
    ]
    return QueryResult(
        columns=columns,
        rows=rows,
        truncated=len(fetched) > query.max_rows,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
