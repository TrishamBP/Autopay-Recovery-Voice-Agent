"""`customer_note`: canonical store for individual notes (untrusted context, never instructions).

`customer.customer_notes` / `customer.notes_updated_at` are a derived digest of these rows,
rewritten in the same transaction as every note change.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.common.enums import NoteCreatedBy, NoteType
from src.common.types import UTCDateTime, enum_column
from src.database import Base

NOTE_MAX_LENGTH = 500


class CustomerNote(Base):
    __tablename__ = "customer_note"
    __table_args__ = (
        CheckConstraint("note_id LIKE 'NOTE-%'", name="note_id_format"),
        CheckConstraint(
            f"length(note_text) BETWEEN 1 AND {NOTE_MAX_LENGTH}", name="note_text_length"
        ),
        CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        Index("ix_customer_note_customer_id_created_at", "customer_id", "created_at"),
    )

    note_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    note_type: Mapped[NoteType] = mapped_column(enum_column(NoteType, "note_type"), index=True)
    note_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[NoteCreatedBy] = mapped_column(enum_column(NoteCreatedBy, "created_by"))
    source_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_session.id", ondelete="SET NULL"), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
