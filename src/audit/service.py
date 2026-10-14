"""Audit trail writer. Called inside the same transaction as the mutation it records."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.models import AuditLog
from src.common.enums import ActorType


def record_audit(
    session: AsyncSession,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor_type: ActorType,
    call_session_id: str | None = None,
    details: dict[str, object] | None = None,
) -> AuditLog:
    entry = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type=actor_type,
        call_session_id=call_session_id,
        details=details,
    )
    session.add(entry)
    return entry
