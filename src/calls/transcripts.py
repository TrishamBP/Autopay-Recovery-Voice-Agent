"""Transcript and call-audit queries, plus the retention purge.

Text is redacted again on read (defence in depth: rules may improve after a turn was stored).
`original_text` is never returned by any API. Access and export are written to `audit_log`.

Retention: after TRANSCRIPT_RETENTION_DAYS (counted from the call's end, or creation if it
never ended) the call's `conversation_turn` rows are deleted and `transcript_purged_at` is set.
`call_event` rows (metadata only, no transcript text), the call session, tool executions and
payment/recovery records are kept, so relational integrity and the audit trail survive.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.service import record_audit
from src.billing.models import BillingInstallment
from src.calls.models import CallEvent, CallSession, ConversationTurn
from src.common.enums import ActorType, CallStatus, TurnRole
from src.common.exceptions import NotFoundError
from src.common.redaction import redact_transcript
from src.recovery.models import RecoveryCase
from src.tools.models import ToolExecution

SPEAKER = {
    TurnRole.CUSTOMER: "CUSTOMER",
    TurnRole.AGENT: "AGENT",
    TurnRole.SYSTEM: "SYSTEM",
    TurnRole.TOOL: "SYSTEM",
}


def transcript_status(call: CallSession, turn_count: int) -> str:
    if call.transcript_purged_at is not None:
        return "purged"
    if turn_count == 0:
        return "not_started" if call.status in (CallStatus.QUEUED,) else "empty"
    if call.transcript_consent is not None and call.transcript_consent.value == "not_retained":
        return "metadata_only"
    return "available"


async def get_call(session: AsyncSession, call_id: str) -> CallSession:
    call = await session.get(CallSession, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    return call


async def list_calls(
    session: AsyncSession,
    *,
    customer_id: str | None,
    recovery_case_id: str | None,
    status: CallStatus | None,
    created_from: datetime | None,
    created_to: datetime | None,
    scenario_id: str | None,
    simulation_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[tuple[CallSession, int, str | None]], int]:
    turns = (
        select(ConversationTurn.call_session_id, func.count().label("n"))
        .group_by(ConversationTurn.call_session_id)
        .subquery()
    )
    query: Select = (
        select(CallSession, func.coalesce(turns.c.n, 0), BillingInstallment.scenario_id)
        .outerjoin(turns, turns.c.call_session_id == CallSession.id)
        .outerjoin(RecoveryCase, RecoveryCase.recovery_case_id == CallSession.recovery_case_id)
        .outerjoin(
            BillingInstallment, BillingInstallment.installment_id == RecoveryCase.installment_id
        )
    )
    filters = []
    if customer_id:
        filters.append(CallSession.customer_id == customer_id)
    if recovery_case_id:
        filters.append(CallSession.recovery_case_id == recovery_case_id)
    if status:
        filters.append(CallSession.status == status)
    if created_from:
        filters.append(CallSession.created_at >= created_from)
    if created_to:
        filters.append(CallSession.created_at < created_to)
    if scenario_id:
        filters.append(BillingInstallment.scenario_id == scenario_id)
    if simulation_id:
        filters.append(CallSession.simulation_id == simulation_id)
    total = (
        await session.scalar(select(func.count()).select_from(query.where(*filters).subquery()))
        or 0
    )
    rows = (
        await session.execute(
            query.where(*filters)
            .order_by(CallSession.created_at.desc(), CallSession.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(c, int(n), sc) for c, n, sc in rows], total


async def turns_for(session: AsyncSession, call_id: str) -> Sequence[ConversationTurn]:
    return (
        await session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.call_session_id == call_id)
            .order_by(ConversationTurn.sequence)
        )
    ).all()


async def events_for(session: AsyncSession, call_id: str) -> Sequence[CallEvent]:
    return (
        await session.scalars(
            select(CallEvent)
            .where(CallEvent.call_session_id == call_id)
            .order_by(CallEvent.occurred_at, CallEvent.id)
        )
    ).all()


async def tool_executions_for(session: AsyncSession, call_id: str) -> Sequence[ToolExecution]:
    return (
        await session.scalars(
            select(ToolExecution)
            .where(ToolExecution.call_session_id == call_id)
            .order_by(ToolExecution.started_at)
        )
    ).all()


def display_text(turn: ConversationTurn) -> str:
    return redact_transcript(turn.transcript_text or "")


async def record_access(factory: async_sessionmaker[AsyncSession], call_id: str, kind: str) -> None:
    async with factory() as session, session.begin():
        record_audit(
            session,
            event_type=f"transcript.{kind}",
            entity_type="call_session",
            entity_id=call_id,
            actor_type=ActorType.OPERATOR,
        )


async def purge_expired_transcripts(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    retention_days: int,
    dry_run: bool = False,
) -> list[str]:
    cutoff = now - timedelta(days=retention_days)
    async with factory() as session, session.begin():
        ended = func.coalesce(CallSession.ended_at, CallSession.created_at)
        expired = (
            await session.scalars(
                select(CallSession.id).where(
                    CallSession.transcript_purged_at.is_(None),
                    ended < cutoff,
                    or_(
                        CallSession.ended_at.is_not(None),
                        CallSession.status.not_in([CallStatus.QUEUED, CallStatus.IN_PROGRESS]),
                    ),
                )
            )
        ).all()
        if dry_run or not expired:
            await session.rollback()
            return list(expired)
        await session.execute(
            delete(ConversationTurn).where(ConversationTurn.call_session_id.in_(expired))
        )
        for call_id in expired:
            call = await session.get(CallSession, call_id)
            if call is not None:
                call.transcript_purged_at = now
            record_audit(
                session,
                event_type="transcript.purged",
                entity_type="call_session",
                entity_id=call_id,
                actor_type=ActorType.SYSTEM,
                details={"retention_days": retention_days},
            )
    return list(expired)
