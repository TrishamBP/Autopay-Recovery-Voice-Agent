"""Customer-note API contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.common.enums import NoteCreatedBy, NoteType
from src.common.schemas import ApiModel
from src.customer_notes.models import NOTE_MAX_LENGTH


class NoteItem(ApiModel):
    note_id: str
    customer_id: str
    created_at: datetime
    note_type: NoteType
    note_text: str
    created_by: NoteCreatedBy
    source_call_id: str | None
    updated_at: datetime


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_type: NoteType
    note_text: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_text: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)
