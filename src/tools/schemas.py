"""Typed tool contracts.

Argument models never contain `customer_id` or `call_session_id`: those come from server-side
context. `extra="forbid"` makes an LLM-supplied `customer_id` a validation error rather than
something silently trusted.
"""

from datetime import datetime
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.common.enums import NoteType, PaymentLinkPurpose
from src.customer_notes.models import NOTE_MAX_LENGTH


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class NoArgs(ToolArgs):
    pass


class AddCustomerNoteArgs(ToolArgs):
    note_type: NoteType
    note_text: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)


class ScheduleCallbackArgs(ToolArgs):
    requested_at: datetime = Field(
        description="Local wall-clock time in `timezone` (e.g. 2026-10-15T18:00:00), or an "
        "ISO timestamp with offset"
    )
    timezone: str = Field(default="Asia/Kolkata", max_length=64, description="IANA time zone")

    @model_validator(mode="after")
    def _resolve_timezone(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {self.timezone!r}") from exc
        if self.requested_at.tzinfo is None:
            object.__setattr__(self, "requested_at", self.requested_at.replace(tzinfo=zone))
        return self


class RequestPaymentLinkArgs(ToolArgs):
    purpose: PaymentLinkPurpose = PaymentLinkPurpose.PAYMENT


class ToolResult(BaseModel):
    """What the model is told. Only produced after the backend knows the real outcome."""

    tool_name: str
    status: Literal["succeeded", "rejected", "failed"]
    data: dict[str, object] = Field(default_factory=dict)
    error_category: str | None = None
    message: str | None = None
    execution_id: str | None = None
    replayed: bool = False
