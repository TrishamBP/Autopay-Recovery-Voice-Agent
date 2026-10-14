"""Call transcripts and audit (`/api/v1/calls`). Operator API key required on every route.

There is one operator role and no customer-facing access, so authorization is the operator
key; call IDs are random UUIDs, and an unknown ID is a plain 404.
"""

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel

from src.calls import transcripts
from src.calls.models import CallEvent, CallSession, ConversationTurn
from src.common.dependencies import OperatorDep, SessionDep
from src.common.enums import CallStatus
from src.common.schemas import ErrorResponse, Page
from src.tools.models import ToolExecution

router = APIRouter(
    prefix="/calls",
    tags=["calls"],
    dependencies=[OperatorDep],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
CallId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")]


class CallSummary(BaseModel):
    call_id: str
    twilio_call_sid: str | None
    provider: str | None
    customer_id: str
    recovery_case_id: str | None
    simulation_id: str | None
    scenario_id: str | None
    call_status: str
    recovery_outcome: str | None
    conversation_stage: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    turn_count: int
    transcript_status: str


class TranscriptTurn(BaseModel):
    turn_number: int | None
    sequence: int
    speaker: str
    timestamp: datetime
    text: str
    intent: str | None
    resolution_preference: str | None
    action: str | None
    action_status: str | None
    processing_status: str | None
    error_category: str | None
    delivery_status: str | None


class TranscriptResponse(BaseModel):
    call_id: str
    twilio_call_sid: str | None
    recovery_case_id: str | None
    customer_id: str
    call_status: str
    transcript_status: str
    transcript_consent: str | None
    turns: list[TranscriptTurn]


class AuditEvent(BaseModel):
    event_id: str
    event_type: str
    source: str
    occurred_at: datetime
    virtual_time: datetime | None
    turn_number: int | None
    details: dict[str, object] | None


class ActionRecord(BaseModel):
    execution_id: str
    tool_name: str
    status: str
    error_category: str | None
    started_at: datetime
    completed_at: datetime | None


class AuditResponse(BaseModel):
    call_id: str
    events: list[AuditEvent]
    actions: list[ActionRecord]


def _summary(call: CallSession, turns: int, scenario_id: str | None) -> CallSummary:
    return CallSummary(
        call_id=call.id,
        twilio_call_sid=call.twilio_call_sid or call.provider_call_id,
        provider=call.provider,
        customer_id=call.customer_id,
        recovery_case_id=call.recovery_case_id,
        simulation_id=call.simulation_id,
        scenario_id=scenario_id,
        call_status=call.status.value,
        recovery_outcome=call.recovery_outcome.value if call.recovery_outcome else None,
        conversation_stage=call.conversation_stage.value if call.conversation_stage else None,
        created_at=call.created_at,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        turn_count=turns,
        transcript_status=transcripts.transcript_status(call, turns),
    )


def _turn(t: ConversationTurn) -> TranscriptTurn:
    return TranscriptTurn(
        turn_number=t.turn_number,
        sequence=t.sequence,
        speaker=transcripts.SPEAKER[t.role],
        timestamp=t.processed_at or t.received_at or t.created_at,
        text=transcripts.display_text(t),
        intent=t.intent.value if t.intent else None,
        resolution_preference=t.resolution_preference.value if t.resolution_preference else None,
        action=t.backend_action,
        action_status=t.action_status,
        processing_status=t.processing_status.value if t.processing_status else None,
        error_category=t.error_category,
        delivery_status=t.delivery_status.value if t.delivery_status else None,
    )


def _event(e: CallEvent) -> AuditEvent:
    return AuditEvent(
        event_id=e.id,
        event_type=e.event_type.value,
        source=e.source.value,
        occurred_at=e.occurred_at,
        virtual_time=e.virtual_time,
        turn_number=e.turn_number,
        details=e.details,
    )


def _action(x: ToolExecution) -> ActionRecord:
    return ActionRecord(
        execution_id=x.id,
        tool_name=x.tool_name,
        status=x.status.value,
        error_category=x.error_category,
        started_at=x.started_at,
        completed_at=x.completed_at,
    )


async def _transcript(session: SessionDep, call_id: str) -> TranscriptResponse:
    call = await transcripts.get_call(session, call_id)
    turns = await transcripts.turns_for(session, call_id)
    return TranscriptResponse(
        call_id=call.id,
        twilio_call_sid=call.twilio_call_sid or call.provider_call_id,
        recovery_case_id=call.recovery_case_id,
        customer_id=call.customer_id,
        call_status=call.status.value,
        transcript_status=transcripts.transcript_status(call, len(turns)),
        transcript_consent=call.transcript_consent.value if call.transcript_consent else None,
        turns=[_turn(t) for t in turns],
    )


async def _audit(session: SessionDep, call_id: str) -> AuditResponse:
    await transcripts.get_call(session, call_id)
    return AuditResponse(
        call_id=call_id,
        events=[_event(e) for e in await transcripts.events_for(session, call_id)],
        actions=[_action(x) for x in await transcripts.tool_executions_for(session, call_id)],
    )


@router.get("", response_model=Page[CallSummary])
async def list_calls(
    session: SessionDep,
    customer_id: Annotated[str | None, Query(pattern=r"^CUS-\d{4}$")] = None,
    recovery_case_id: Annotated[str | None, Query(max_length=64)] = None,
    status: CallStatus | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    scenario_id: Annotated[str | None, Query(max_length=64)] = None,
    simulation_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CallSummary]:
    rows, total = await transcripts.list_calls(
        session,
        customer_id=customer_id,
        recovery_case_id=recovery_case_id,
        status=status,
        created_from=created_from,
        created_to=created_to,
        scenario_id=scenario_id,
        simulation_id=simulation_id,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[_summary(c, n, sc) for c, n, sc in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{call_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    call_id: CallId, session: SessionDep, request: Request
) -> TranscriptResponse:
    body = await _transcript(session, call_id)
    await session.rollback()
    await transcripts.record_access(request.app.state.session_factory, call_id, "accessed")
    return body


@router.get("/{call_id}/audit", response_model=AuditResponse)
async def get_audit(call_id: CallId, session: SessionDep) -> AuditResponse:
    return await _audit(session, call_id)


@router.get("/{call_id}/transcript/export")
async def export_transcript(call_id: CallId, session: SessionDep, request: Request) -> Response:
    """Authenticated JSON export (transcript + audit) for audit/debugging."""
    transcript = await _transcript(session, call_id)
    audit = await _audit(session, call_id)
    await session.rollback()
    await transcripts.record_access(request.app.state.session_factory, call_id, "exported")
    payload = {
        "transcript": transcript.model_dump(mode="json"),
        "audit": audit.model_dump(mode="json"),
    }
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="call-{call_id}.json"',
            "Cache-Control": "no-store",
        },
    )
