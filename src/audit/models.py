"""`audit_log`: append-only record of every mutation and import run. `details` is redacted
structured metadata; never put secrets, raw audio or full transcripts in it."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.enums import ActorType
from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[ActorType] = mapped_column(enum_column(ActorType, "actor_type"))
    call_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_session.id", ondelete="SET NULL"), index=True
    )
    details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
