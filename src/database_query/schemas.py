"""API contract for the natural-language read-only query endpoint."""

from typing import Literal

from pydantic import BaseModel, Field


class DatabaseQueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    limit: int = Field(default=50, ge=1, le=1000)
    explain: bool = True


class ValidatedSql(BaseModel):
    """SQL regenerated from the validated AST; it ran on a read-only connection."""

    validated_read_only_sql: str
    parameters: dict[str, object]
    tables: list[str]


class DatabaseQueryResponse(BaseModel):
    question: str
    query: ValidatedSql
    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    duration_ms: float
    explanation: str | None
    explanation_status: Literal["ok", "skipped", "unavailable"]


class TableCount(BaseModel):
    table: str
    rows: int


class DatabaseSummaryResponse(BaseModel):
    tables: list[TableCount]
    customers_by_recovery_status: dict[str, int]
    customers_by_payment_status: dict[str, int]
    payment_history_by_status: dict[str, int]
    do_not_contact_customers: int
    alembic_revision: str | None
