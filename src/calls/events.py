"""Call event recorder. Called inside the transaction of the change it describes.

Idempotent: an event with an existing `idempotency_key` is not written again, so repeated
Twilio deliveries and webhook retries produce one event. Never store transcript text, secrets
or raw webhook bodies in `details`.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.calls.models import CallEvent, CallSession
from src.common.time import utc_now
from src.voice.vocabulary import CallEventType, EventSource

# Twilio CallStatus -> lifecycle event (queued/initiated carry no new information).
TWILIO_STATUS_EVENTS: dict[str, CallEventType] = {
    "ringing": CallEventType.CALL_RINGING,
    "in-progress": CallEventType.CALL_ANSWERED,
    "answered": CallEventType.CALL_ANSWERED,
    "completed": CallEventType.CALL_COMPLETED,
    "busy": CallEventType.CALL_BUSY,
    "no-answer": CallEventType.CALL_NO_ANSWER,
    "failed": CallEventType.CALL_FAILED,
    "canceled": CallEventType.CALL_FAILED,
}


async def record_call_event(
    session: AsyncSession,
    call: CallSession,
    event_type: CallEventType,
    *,
    source: EventSource,
    key: str,
    details: dict[str, object] | None = None,
    turn_number: int | None = None,
    virtual_time: datetime | None = None,
) -> CallEvent | None:
    idempotency_key = f"{call.id}:{key}"
    if await session.scalar(
        select(CallEvent.id).where(CallEvent.idempotency_key == idempotency_key)
    ):
        return None
    event = CallEvent(
        call_session_id=call.id,
        recovery_case_id=call.recovery_case_id,
        customer_id=call.customer_id,
        turn_number=turn_number,
        event_type=event_type,
        source=source,
        occurred_at=utc_now(),
        virtual_time=virtual_time,
        details=details,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    return event
