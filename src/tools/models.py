"""`tool_execution`: one row per typed-tool invocation, including rejected ones.

`arguments` holds the *validated* arguments only; `result_summary` is redacted (no link tokens,
URLs, phone numbers). `(tool_name, idempotency_key)` is unique so a replayed side effect is
returned from this table instead of executing twice.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.enums import ToolExecutionStatus
from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base


class ToolExecution(Base):
    __tablename__ = "tool_execution"
    __table_args__ = (
        UniqueConstraint("tool_name", "idempotency_key"),
        Index("ix_tool_execution_customer_id_started_at", "customer_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_session.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT")
    )
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    arguments: Mapped[dict[str, object] | None] = mapped_column(JSON)
    status: Mapped[ToolExecutionStatus] = mapped_column(enum_column(ToolExecutionStatus, "status"))
    result_summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_category: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
