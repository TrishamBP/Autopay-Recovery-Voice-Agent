"""Manual approval of a recovery call. The ONLY code path that can initiate a call.

Flow (no transaction is ever held across the provider request):
1. Claim: conditional UPDATE of the case (status in APPROVABLE -> APPROVED). This takes SQLite's
   write lock, so two concurrent approvals cannot both win. Inside the same transaction every
   rule is re-validated against fresh rows; any failure rolls the claim back.
   Then a `call_session` row is created; a partial unique index allows one active call per case.
2. Pre-call re-check in a short transaction (payment may have succeeded meanwhile).
3. Provider request (mock by default) outside any transaction.
4. Record the real outcome: CALL_INITIATED, CALL_FAILED (definitive) or CALL_INITIATION_UNKNOWN
   (timeout; reconciled later by a status callback, never retried blindly).
A call result never changes payment status.
"""

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.service import record_audit
from src.billing.models import BillingInstallment
from src.billing.state_machine import UNPAID, InstallmentStatus
from src.calls.events import record_call_event
from src.calls.models import CallSession
from src.common.enums import ActorType, CallStatus
from src.common.exceptions import AppError, ConflictError, NotFoundError
from src.common.redaction import mask_phone
from src.common.time import utc_now
from src.config import Settings
from src.customers.models import Customer
from src.recovery import service as recovery
from src.recovery.models import RecoveryCase
from src.recovery.policy import (
    Denial,
    consent_denial,
    contact_window_denial,
    destination_denial,
)
from src.recovery.state_machine import APPROVABLE, TERMINAL, RecoveryOutcome
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.simulation.events import EventType
from src.simulation.ledger import record_fact
from src.simulation.models import SimulationState
from src.telephony.base import TelephonyError, TelephonyOutcomeUnknownError, TelephonyProvider
from src.voice.vocabulary import CallEventType, EventSource

logger = logging.getLogger(__name__)


class ApprovalDenied(AppError):
    status_code = 409
    code = "approval_denied"


class CallInitiationFailed(AppError):
    status_code = 502
    code = "call_initiation_failed"


@dataclass(frozen=True)
class ApprovalRequest:
    approved_by: str
    approval_reason: str
    destination: str
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ApprovalResult:
    recovery_case_id: str
    status: R
    call_session_id: str
    provider: str
    provider_call_id: str | None
    call_initiation: str  # initiated | unknown
    replayed: bool = False


def _deny(denial: Denial, **details: object) -> ApprovalDenied:
    return ApprovalDenied(denial.message, details={"reason": denial.code, **details})


async def _record_denial(
    factory: async_sessionmaker[AsyncSession],
    case_id: str,
    denial: Denial,
    *,
    opt_out: bool = False,
    cancel_paid: bool = False,
) -> None:
    """Persist side effects of a failed check (audit; opt-out / paid cancellation)."""
    async with factory() as session, session.begin():
        case = await session.get(RecoveryCase, case_id)
        if case is None:
            return
        sim = await session.get(SimulationState, case.simulation_id)
        now_virtual = sim.current_virtual_time if sim else utc_now()
        if opt_out and case.status not in TERMINAL:
            recovery.opt_out(session, case, virtual_time=now_virtual, actor_type=ActorType.OPERATOR)
        if cancel_paid and case.status not in TERMINAL:
            installment = await session.get(BillingInstallment, case.installment_id)
            if installment is not None:
                await recovery.end_case_for_payment(
                    session, installment, already_paid=False, virtual_time=now_virtual
                )
        record_audit(
            session,
            event_type="recovery.approval_denied",
            entity_type="recovery_case",
            entity_id=case_id,
            actor_type=ActorType.OPERATOR,
            details={"reason": denial.code},
        )


def _validate(
    case: RecoveryCase,
    installment: BillingInstallment,
    customer: Customer,
    sim: SimulationState,
    destination: str,
    settings: Settings,
) -> tuple[Denial, dict[str, bool]] | None:
    if sim.current_virtual_time < case.eligible_at:
        return Denial("not_yet_eligible", "The recovery case is not eligible yet"), {}
    if installment.payment_status == InstallmentStatus.SUCCESS:
        return Denial("installment_paid", "The installment has already been paid"), {
            "cancel_paid": True
        }
    if installment.payment_status not in UNPAID:
        return Denial(
            "payment_pending_reconciliation",
            "The payment outcome is not final (pending reconciliation)",
        ), {}
    if customer.do_not_contact:
        return Denial("do_not_contact", "The customer asked not to be contacted"), {"opt_out": True}
    for check in (
        consent_denial(customer, settings),
        destination_denial(destination, settings),
        contact_window_denial(customer, sim.current_virtual_time, ZoneInfo(sim.timezone), settings),
    ):
        if check is not None:
            return check, {}
    return None


async def approve_case(
    factory: async_sessionmaker[AsyncSession],
    provider: TelephonyProvider,
    settings: Settings,
    case_id: str,
    request: ApprovalRequest,
) -> ApprovalResult:
    if request.idempotency_key:
        async with factory() as session:
            existing = await session.scalar(
                select(CallSession).where(CallSession.idempotency_key == request.idempotency_key)
            )
            if existing is not None:
                if existing.recovery_case_id != case_id:
                    raise ConflictError("Idempotency key already used for another case")
                case = await session.get(RecoveryCase, case_id)
                return ApprovalResult(
                    case_id,
                    case.status if case else R.APPROVED,
                    existing.id,
                    existing.provider or "",
                    existing.provider_call_id,
                    "replayed",
                    replayed=True,
                )
    denial = destination_denial(request.destination, settings)
    if denial is not None:
        raise _deny(denial)
    async with factory() as session:  # never dial a (fictional) customer record's number
        if await session.scalar(
            select(Customer.customer_id).where(Customer.phone_number == request.destination)
        ):
            raise _deny(
                Denial(
                    "destination_is_customer_record",
                    "Customer record numbers are never dialled; use a test number",
                )
            )

    # 1. Atomic claim + full re-validation in one transaction.
    pending_denial: tuple[Denial, dict[str, bool]] | None = None
    async with factory() as session:
        case = await session.get(RecoveryCase, case_id)
        if case is None:
            raise NotFoundError(f"Recovery case {case_id} not found")
        if case.status not in APPROVABLE:
            raise ApprovalDenied(
                "The recovery case is not awaiting approval",
                details={"reason": "case_not_awaiting_approval", "status": case.status.value},
            )
        loaded_status = case.status
        await session.rollback()
        try:
            async with session.begin():
                claimed = await session.execute(
                    update(RecoveryCase)
                    .where(
                        RecoveryCase.recovery_case_id == case_id,
                        RecoveryCase.status == loaded_status,
                    )
                    .values(status=R.APPROVED)
                )
                if claimed.rowcount != 1:  # type: ignore[attr-defined]
                    raise ApprovalDenied(
                        "Another approval for this case is already in progress",
                        details={"reason": "concurrent_approval"},
                    )
                case = await session.get(RecoveryCase, case_id, populate_existing=True)
                installment = (
                    await session.get(BillingInstallment, case.installment_id) if case else None
                )
                customer = await session.get(Customer, case.customer_id) if case else None
                sim = await session.get(SimulationState, case.simulation_id) if case else None
                if case is None or installment is None or customer is None or sim is None:
                    raise NotFoundError("Recovery case data is incomplete")
                problem = _validate(case, installment, customer, sim, request.destination, settings)
                if problem is not None:
                    pending_denial = problem
                    raise _deny(problem[0])
                case.status = loaded_status  # re-apply through the state machine for auditing
                recovery.transition(
                    session,
                    case,
                    R.APPROVED,
                    actor_type=ActorType.OPERATOR,
                    reason="manual_approval",
                    details={"approved_by": request.approved_by},
                )
                case.approved_at = utc_now()
                case.approved_at_virtual = sim.current_virtual_time
                case.approved_by = request.approved_by
                case.approval_reason = request.approval_reason
                call = CallSession(
                    customer_id=case.customer_id,
                    recovery_case_id=case_id,
                    simulation_id=case.simulation_id,
                    status=CallStatus.QUEUED,
                    provider=provider.name,
                    destination_masked=mask_phone(request.destination),
                    idempotency_key=request.idempotency_key,
                )
                session.add(call)
                await session.flush()
                call_id = call.id
                await record_fact(
                    session,
                    simulation_id=case.simulation_id,
                    event_type=EventType.RECOVERY_APPROVED,
                    aggregate_type="recovery_case",
                    aggregate_id=case_id,
                    discriminator=f"call-{call_id}",
                    virtual_time=sim.current_virtual_time,
                    payload={"approved_by": request.approved_by, "call_session_id": call_id},
                )
                await record_call_event(
                    session,
                    call,
                    CallEventType.CALL_APPROVED,
                    source=EventSource.BACKEND,
                    key="approved",
                    virtual_time=sim.current_virtual_time,
                    details={"approved_by": request.approved_by},
                )
                record_audit(
                    session,
                    event_type="recovery.approved",
                    entity_type="recovery_case",
                    entity_id=case_id,
                    actor_type=ActorType.OPERATOR,
                    details={
                        "approved_by": request.approved_by,
                        "reason": request.approval_reason,
                        "destination": mask_phone(request.destination),
                        "call_session_id": call_id,
                    },
                )
        except IntegrityError as exc:
            raise ApprovalDenied(
                "An active call already exists for this case",
                details={"reason": "duplicate_active_call"},
            ) from exc
        except ApprovalDenied:
            # The claim was rolled back; persist the denial's audit and side effects.
            if pending_denial is not None:
                denial, flags = pending_denial
                await _record_denial(
                    factory,
                    case_id,
                    denial,
                    opt_out=flags.get("opt_out", False),
                    cancel_paid=flags.get("cancel_paid", False),
                )
            raise

    # 2. Short pre-call re-check (state may have changed since the claim committed).
    async with factory() as session, session.begin():
        case = await session.get(RecoveryCase, case_id)
        installment = await session.get(BillingInstallment, case.installment_id) if case else None
        customer = await session.get(Customer, case.customer_id) if case else None
        call = await session.get(CallSession, call_id)
        sim = await session.get(SimulationState, case.simulation_id) if case else None
        at = sim.current_virtual_time if sim else utc_now()
        problem: Denial | None = None
        if case is None or case.status != R.APPROVED:
            problem = Denial("case_changed", "The case changed before the call was placed")
        elif installment is None or installment.payment_status not in UNPAID:
            problem = Denial("installment_paid", "The installment is no longer unpaid")
        elif customer is None or customer.do_not_contact:
            problem = Denial("do_not_contact", "The customer asked not to be contacted")
        if problem is not None:
            if call is not None:
                call.status, call.termination_reason = CallStatus.FAILED, f"precheck:{problem.code}"
            if case is not None and case.status == R.APPROVED and problem.code == "do_not_contact":
                recovery.opt_out(session, case, virtual_time=at, actor_type=ActorType.SYSTEM)
            elif (
                case is not None and installment is not None and problem.code == "installment_paid"
            ):
                await recovery.end_case_for_payment(
                    session, installment, already_paid=False, virtual_time=at
                )
        if problem is None and call is not None:
            await record_call_event(
                session,
                call,
                CallEventType.CALL_INITIATION_REQUESTED,
                source=EventSource.BACKEND,
                key="initiation-requested",
                virtual_time=at,
                details={"provider": provider.name},
            )
    if problem is not None:
        raise _deny(problem)

    # 3. Provider request: outside any transaction.
    error: Exception | None = None
    try:
        result = await provider.initiate_call(
            recovery_case_id=case_id,
            call_session_id=call_id,
            destination=request.destination,
            idempotency_key=request.idempotency_key or call_id,
        )
    except (TelephonyError, TelephonyOutcomeUnknownError) as exc:
        error, result = exc, None

    # 4. Record the actual outcome.
    async with factory() as session, session.begin():
        case = await session.get(RecoveryCase, case_id)
        call = await session.get(CallSession, call_id)
        sim = await session.get(SimulationState, case.simulation_id) if case else None
        if case is None or call is None or sim is None:
            raise NotFoundError("Recovery case disappeared during call initiation")
        at = sim.current_virtual_time
        if result is not None:
            call.provider_call_id = result.provider_call_id
            if result.provider == "twilio":
                call.twilio_call_sid = result.provider_call_id
            recovery.transition(session, case, R.CALL_INITIATED, actor_type=ActorType.SYSTEM)
            event, outcome, status = (
                EventType.CALL_INITIATION_REQUESTED,
                "initiated",
                R.CALL_INITIATED,
            )
        elif isinstance(error, TelephonyOutcomeUnknownError):
            call.termination_reason = "initiation_outcome_unknown"
            recovery.transition(
                session, case, R.CALL_INITIATION_UNKNOWN, actor_type=ActorType.SYSTEM
            )
            event, outcome, status = (
                EventType.CALL_INITIATION_REQUESTED,
                "unknown",
                R.CALL_INITIATION_UNKNOWN,
            )
        else:
            call.status = CallStatus.FAILED
            call.termination_reason = f"provider_rejected: {error}"[:200]
            recovery.transition(session, case, R.CALL_FAILED, actor_type=ActorType.SYSTEM)
            event, outcome, status = EventType.CALL_INITIATION_FAILED, "failed", R.CALL_FAILED
        await record_fact(
            session,
            simulation_id=case.simulation_id,
            event_type=event,
            aggregate_type="call_session",
            aggregate_id=call_id,
            discriminator=outcome,
            virtual_time=at,
            payload={"recovery_case_id": case_id, "provider": provider.name},
        )
        lifecycle = {
            "initiated": CallEventType.CALL_INITIATED,
            "unknown": CallEventType.CALL_INITIATION_UNKNOWN,
            "failed": CallEventType.CALL_FAILED,
        }[outcome]
        await record_call_event(
            session,
            call,
            lifecycle,
            source=EventSource.BACKEND,
            key=f"initiation:{outcome}",
            virtual_time=at,
            details={"provider": provider.name},
        )
        record_audit(
            session,
            event_type=f"call.initiation_{outcome}",
            entity_type="call_session",
            entity_id=call_id,
            actor_type=ActorType.SYSTEM,
            details={"recovery_case_id": case_id, "provider": provider.name},
        )
    logger.info(
        "recovery call initiation",
        extra={"recovery_case_id": case_id, "outcome": outcome, "provider": provider.name},
    )
    if outcome == "failed":
        raise CallInitiationFailed(
            "The telephony provider did not place the call",
            details={"call_session_id": call_id, "recovery_status": status.value},
        )
    return ApprovalResult(
        case_id,
        status,
        call_id,
        provider.name,
        result.provider_call_id if result else None,
        outcome,
    )


async def reconcile_call(
    factory: async_sessionmaker[AsyncSession],
    provider: TelephonyProvider,
    settings: Settings,
    case_id: str,
    destination: str,
) -> ApprovalResult:
    """Resolve CALL_INITIATION_UNKNOWN by asking the provider whether the call exists.

    Found -> CALL_INITIATED with the provider id. Not found after the grace period ->
    CALL_FAILED (a NEW explicit approval may then retry). Too early -> 409, nothing changes.
    The operator re-supplies the allowlisted destination; it must match the stored mask.
    """
    denial = destination_denial(destination, settings)
    if denial is not None:
        raise _deny(denial)
    async with factory() as session:
        case = await session.get(RecoveryCase, case_id)
        if case is None:
            raise NotFoundError(f"Recovery case {case_id} not found")
        if case.status != R.CALL_INITIATION_UNKNOWN:
            raise ApprovalDenied(
                "Only calls with an unknown initiation outcome can be reconciled",
                details={"reason": "not_ambiguous", "status": case.status.value},
            )
        call = await session.scalar(
            select(CallSession).where(
                CallSession.recovery_case_id == case_id,
                CallSession.status.in_([CallStatus.QUEUED, CallStatus.IN_PROGRESS]),
            )
        )
        if call is None or call.destination_masked != mask_phone(destination):
            raise ApprovalDenied(
                "No matching ambiguous call for this destination",
                details={"reason": "no_matching_call"},
            )
        call_id, created = call.id, call.created_at
    found = await provider.find_call(
        call_session_id=call_id, destination=destination, created_after=created
    )  # network: outside any transaction
    async with factory() as session, session.begin():
        case = await session.get(RecoveryCase, case_id)
        call = await session.get(CallSession, call_id)
        if case is None or call is None or case.status != R.CALL_INITIATION_UNKNOWN:
            raise ApprovalDenied(
                "The call changed during reconciliation", details={"reason": "case_changed"}
            )
        if found:
            call.provider_call_id = call.provider_call_id or found
            if call.provider == "twilio":
                call.twilio_call_sid = call.twilio_call_sid or found
            recovery.transition(
                session,
                case,
                R.CALL_INITIATED,
                actor_type=ActorType.OPERATOR,
                reason="reconciled_found",
            )
            outcome = "initiated"
            await record_call_event(
                session,
                call,
                CallEventType.CALL_INITIATED,
                source=EventSource.BACKEND,
                key="reconciled:initiated",
                details={"reconciled": True},
            )
        elif (utc_now() - created).total_seconds() < settings.telephony_reconcile_grace_seconds:
            raise ApprovalDenied(
                "Too early to conclude that no call was created; retry later",
                details={"reason": "reconcile_too_early"},
            )
        else:
            call.status, call.termination_reason = CallStatus.FAILED, "reconciled_no_call"
            recovery.transition(
                session,
                case,
                R.CALL_FAILED,
                actor_type=ActorType.OPERATOR,
                reason="reconciled_not_found",
            )
            outcome = "failed"
            await record_call_event(
                session,
                call,
                CallEventType.CALL_FAILED,
                source=EventSource.BACKEND,
                key="reconciled:failed",
                details={"reconciled": True, "reason": "no_call_found"},
            )
        record_audit(
            session,
            event_type=f"call.reconciled_{outcome}",
            entity_type="call_session",
            entity_id=call_id,
            actor_type=ActorType.OPERATOR,
            details={"recovery_case_id": case_id},
        )
        status = case.status
    return ApprovalResult(case_id, status, call_id, provider.name, found, outcome)


async def reject_case(
    factory: async_sessionmaker[AsyncSession],
    case_id: str,
    *,
    rejected_by: str,
    reason: str,
) -> RecoveryCase:
    async with factory() as session, session.begin():
        case = await session.get(RecoveryCase, case_id)
        if case is None:
            raise NotFoundError(f"Recovery case {case_id} not found")
        if case.status not in APPROVABLE:
            raise ApprovalDenied(
                "Only cases awaiting approval can be rejected",
                details={"reason": "case_not_awaiting_approval", "status": case.status.value},
            )
        sim = await session.get(SimulationState, case.simulation_id)
        recovery.transition(
            session,
            case,
            R.REJECTED,
            actor_type=ActorType.OPERATOR,
            reason=reason,
            details={"rejected_by": rejected_by},
        )
        case.recovery_outcome = RecoveryOutcome.REJECTED_BY_OPERATOR
        case.cancellation_reason = "rejected_by_operator"
        if sim is not None:
            case.cancelled_at = sim.current_virtual_time
            await record_fact(
                session,
                simulation_id=case.simulation_id,
                event_type=EventType.RECOVERY_REJECTED,
                aggregate_type="recovery_case",
                aggregate_id=case_id,
                discriminator="rejected",
                virtual_time=sim.current_virtual_time,
                payload={"rejected_by": rejected_by},
            )
    return case
