"""Recovery-case domain operations. Callers own the transaction; no network I/O here."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import record_audit
from src.billing.models import BillingInstallment
from src.common.enums import ActorType
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import (
    ACTIVE,
    CONTACTED,
    RecoveryCaseStatus,
    RecoveryOutcome,
    check_transition,
)
from src.simulation.events import EventType, recovery_case_key
from src.simulation.ledger import record_fact

R = RecoveryCaseStatus


def transition(
    session: AsyncSession,
    case: RecoveryCase,
    target: RecoveryCaseStatus,
    *,
    actor_type: ActorType,
    reason: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    previous = case.status
    case.status = check_transition(previous, target)
    record_audit(
        session,
        event_type="recovery_case.transition",
        entity_type="recovery_case",
        entity_id=case.recovery_case_id,
        actor_type=actor_type,
        details={"from": previous.value, "to": target.value, "reason": reason} | (details or {}),
    )


async def get_active_case(session: AsyncSession, installment_id: str) -> RecoveryCase | None:
    return await session.scalar(
        select(RecoveryCase).where(
            RecoveryCase.installment_id == installment_id,
            RecoveryCase.status.in_(list(ACTIVE)),
        )
    )


async def open_case(
    session: AsyncSession,
    installment: BillingInstallment,
    *,
    failed_at: datetime,
    eligible_at: datetime,
) -> RecoveryCase | None:
    """Open the single recovery case for a definitively failed installment (idempotent)."""
    key = recovery_case_key(installment.installment_id)
    if await session.scalar(
        select(RecoveryCase.recovery_case_id).where(RecoveryCase.idempotency_key == key)
    ):
        return None
    case = RecoveryCase(
        simulation_id=installment.simulation_id,
        installment_id=installment.installment_id,
        customer_id=installment.customer_id,
        status=R.OPEN,
        opened_at_virtual=failed_at,
        eligible_at=eligible_at,
        idempotency_key=key,
    )
    session.add(case)
    await session.flush()
    installment.recovery_eligible_at = eligible_at
    await record_fact(
        session,
        simulation_id=installment.simulation_id,
        event_type=EventType.RECOVERY_CASE_OPENED,
        aggregate_type="recovery_case",
        aggregate_id=case.recovery_case_id,
        discriminator="opened",
        virtual_time=failed_at,
        payload={
            "installment_id": installment.installment_id,
            "eligible_at": eligible_at.isoformat(),
        },
    )
    record_audit(
        session,
        event_type="recovery_case.opened",
        entity_type="recovery_case",
        entity_id=case.recovery_case_id,
        actor_type=ActorType.SYSTEM,
        details={
            "installment_id": installment.installment_id,
            "eligible_at": eligible_at.isoformat(),
        },
    )
    return case


async def end_case_for_payment(
    session: AsyncSession,
    installment: BillingInstallment,
    *,
    already_paid: bool,
    virtual_time: datetime,
) -> RecoveryCase | None:
    """A successful / already-paid payment ends any active case: CLOSED if the customer was
    already contacted (outcome recorded), otherwise CANCELLED."""
    case = await get_active_case(session, installment.installment_id)
    if case is None:
        return None
    contacted = case.status in CONTACTED or case.status == R.APPROVED
    outcome = (
        RecoveryOutcome.PAYMENT_RECEIVED_AFTER_CONTACT
        if contacted
        else RecoveryOutcome.ALREADY_PAID
        if already_paid
        else RecoveryOutcome.PAYMENT_SUCCEEDED
    )
    target = R.CLOSED if contacted else R.CANCELLED
    transition(session, case, target, actor_type=ActorType.SYSTEM, reason=outcome.value)
    case.recovery_outcome = outcome
    case.cancelled_at = virtual_time
    case.cancellation_reason = outcome.value
    await record_fact(
        session,
        simulation_id=case.simulation_id,
        event_type=EventType.RECOVERY_CANCELLED,
        aggregate_type="recovery_case",
        aggregate_id=case.recovery_case_id,
        discriminator=target.value,
        virtual_time=virtual_time,
        payload={"status": target.value, "outcome": outcome.value},
    )
    return case


def opt_out(
    session: AsyncSession, case: RecoveryCase, *, virtual_time: datetime, actor_type: ActorType
) -> None:
    """Customer asked for no further contact: distinct terminal state, not a generic cancel."""
    transition(session, case, R.OPTED_OUT, actor_type=actor_type, reason="do_not_contact")
    case.recovery_outcome = RecoveryOutcome.DO_NOT_CONTACT
    case.cancelled_at = virtual_time
    case.cancellation_reason = "do_not_contact"


async def list_cases(
    session: AsyncSession,
    *,
    simulation_id: str,
    statuses: Sequence[RecoveryCaseStatus] | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[RecoveryCase], int]:
    conditions = [RecoveryCase.simulation_id == simulation_id]
    if statuses:
        conditions.append(RecoveryCase.status.in_(list(statuses)))
    total = await session.scalar(select(func.count()).select_from(RecoveryCase).where(*conditions))
    rows = await session.scalars(
        select(RecoveryCase)
        .where(*conditions)
        .order_by(RecoveryCase.eligible_at, RecoveryCase.recovery_case_id)
        .limit(limit)
        .offset(offset)
    )
    return rows.all(), total or 0
