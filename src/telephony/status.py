"""Call status updates (Twilio status callbacks or the dev-only mock endpoint).

Safe for duplicate and out-of-order delivery: a status never moves backwards, a terminal status
is final, and a repeated/older `SequenceNumber` is ignored. A completed call does NOT mean the
payment was recovered; only payment events change payment or close a case as recovered.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.service import record_audit
from src.calls.events import TWILIO_STATUS_EVENTS, record_call_event
from src.calls.models import CallSession
from src.common.enums import ActorType, CallStatus
from src.common.exceptions import NotFoundError, ValidationFailedError
from src.common.time import utc_now
from src.recovery import service as recovery
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.simulation.events import EventType
from src.simulation.ledger import record_fact
from src.simulation.models import SimulationState
from src.voice.vocabulary import CallEventType, ConversationStage, EventSource

PROVIDER_STATUS = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.QUEUED,
    "in-progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "no-answer": CallStatus.NO_ANSWER,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.FAILED,
}
RANK = {CallStatus.QUEUED: 1, CallStatus.IN_PROGRESS: 2}
TERMINAL_EVENTS = frozenset(
    {
        CallEventType.CALL_COMPLETED,
        CallEventType.CALL_BUSY,
        CallEventType.CALL_NO_ANSWER,
        CallEventType.CALL_FAILED,
    }
)
TERMINAL_CALL = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.BUSY,
        CallStatus.NO_ANSWER,
        CallStatus.FAILED,
        CallStatus.VOICEMAIL,
    }
)


async def apply_call_status(
    factory: async_sessionmaker[AsyncSession],
    *,
    call_session_id: str,
    provider_status: str,
    provider_call_id: str | None,
    sequence: int | None,
    duration_seconds: int | None = None,
) -> str:
    new_status = PROVIDER_STATUS.get(provider_status.lower())
    if new_status is None:
        raise ValidationFailedError(f"unsupported call status {provider_status!r}")
    async with factory() as session, session.begin():
        call = await session.get(CallSession, call_session_id)
        if call is None:
            raise NotFoundError("Unknown call session")
        if provider_call_id and call.provider_call_id and provider_call_id != call.provider_call_id:
            raise ValidationFailedError("Call SID does not match this call session")
        event_type = TWILIO_STATUS_EVENTS.get(provider_status.lower())
        if event_type is not None:  # "Twilio reported X" is recorded once per status, even if late
            await record_call_event(
                session,
                call,
                event_type,
                source=EventSource.TWILIO,
                key=f"status:{event_type.value}",
                details={"sequence": sequence, "duration": duration_seconds},
            )
        if event_type in TERMINAL_EVENTS:
            await record_call_event(
                session,
                call,
                CallEventType.CONVERSATION_ENDED,
                source=EventSource.TWILIO,
                key="conversation:ended",
                details={"reason": provider_status.lower()},
            )
        if (
            sequence is not None
            and call.last_status_sequence is not None
            and (sequence <= call.last_status_sequence)
        ):
            return "ignored:duplicate_or_stale"
        if call.status in TERMINAL_CALL:
            return "ignored:already_terminal"
        if new_status not in TERMINAL_CALL and RANK.get(new_status, 0) <= RANK.get(call.status, 0):
            return "ignored:no_progress"
        if provider_call_id and call.provider_call_id is None:  # reconciles an unknown initiation
            call.provider_call_id = provider_call_id
            if call.provider == "twilio":
                call.twilio_call_sid = provider_call_id
        call.status = new_status
        if new_status in TERMINAL_CALL:
            call.conversation_stage = ConversationStage.ENDED
        now = utc_now()
        if new_status == CallStatus.IN_PROGRESS and call.started_at is None:
            call.started_at = now
        if new_status in TERMINAL_CALL:
            call.ended_at = now if call.started_at is None or now >= call.started_at else None
            if duration_seconds is not None and duration_seconds >= 0:
                call.duration_seconds = duration_seconds
        if sequence is not None:
            call.last_status_sequence = sequence
        await _update_case(session, call, new_status)
        record_audit(
            session,
            event_type="call.status_updated",
            entity_type="call_session",
            entity_id=call.id,
            actor_type=ActorType.SYSTEM,
            details={"status": new_status.value, "sequence": sequence},
        )
        return f"applied:{new_status.value}"


async def _update_case(session: AsyncSession, call: CallSession, status: CallStatus) -> None:
    if call.recovery_case_id is None:
        return
    case = await session.get(RecoveryCase, call.recovery_case_id)
    if case is None:
        return
    if case.status == R.CALL_INITIATION_UNKNOWN:  # callback proves the call exists
        recovery.transition(
            session,
            case,
            R.CALL_INITIATED,
            actor_type=ActorType.SYSTEM,
            reason="reconciled_by_status_callback",
        )
    if status in TERMINAL_CALL and case.status == R.CALL_INITIATED:
        target = R.CALL_COMPLETED if status == CallStatus.COMPLETED else R.CALL_FAILED
        recovery.transition(
            session, case, target, actor_type=ActorType.SYSTEM, reason=f"call_{status.value}"
        )
    sim = await session.get(SimulationState, case.simulation_id)
    if sim is not None:
        await record_fact(
            session,
            simulation_id=sim.simulation_id,
            event_type=EventType.CALL_STATUS_UPDATED,
            aggregate_type="call_session",
            aggregate_id=call.id,
            discriminator=f"{status.value}",
            virtual_time=sim.current_virtual_time,
            payload={"status": status.value, "recovery_case_id": case.recovery_case_id},
        )
