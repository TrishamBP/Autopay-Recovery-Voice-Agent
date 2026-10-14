"""Sequential, durable event processor driven by the virtual clock.

Transaction & recovery strategy (SQLite, single backend process):
1. Materialise INSTALLMENT_DUE events for every due date in [initial_time, target] (insert-if-
   absent by idempotency key) in one transaction.
2. Repeatedly, in ONE transaction per event: pick the earliest event <= target by
   (scheduled_at_virtual, sequence); claim it with a conditional UPDATE (takes SQLite's write
   lock first, so the rest of the transaction reads a stable state); apply its effects; insert
   follow-up events; mark it processed; move the clock to the event's time; bump the advance
   counters; commit.
3. When no event <= target remains, move the clock to target and complete the advance.

A crash at any point leaves the clock at the last *fully applied* event: effects, the processed
flag and the clock commit together. Re-issuing the advance resumes from there; every event and
entity is keyed by a deterministic idempotency key, so nothing is duplicated.
A handler error rolls back that event, increments attempt_count and records last_error in a
separate transaction. After SIMULATION_MAX_EVENT_ATTEMPTS the event is dead-lettered (FAILED)
and the clock refuses to pass it until an operator retries it. No network I/O happens here: no
LLM, no telephony. Calls are never placed by the clock.
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.billing.models import BillingInstallment, PaymentAttempt, Subscription
from src.billing.state_machine import AttemptOutcome, PaymentSource, check_transition
from src.billing.state_machine import InstallmentStatus as I
from src.common.enums import ActorType
from src.common.exceptions import AppError, ConflictError, NotFoundError
from src.common.time import utc_now
from src.config import Settings
from src.customers.models import Customer
from src.recovery import service as recovery
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.simulation.calendar import due_dates, local_instant
from src.simulation.events import (
    AdvanceStatus,
    EventStatus,
    EventType,
    SimulationStatus,
    attempt_key,
    installment_key,
)
from src.simulation.ledger import schedule_event
from src.simulation.models import (
    ALL_PERIODS,
    ScenarioAssignment,
    SimulationAdvance,
    SimulationEvent,
    SimulationState,
)
from src.simulation.scenarios import CATALOG, seeded_scenario_id

logger = logging.getLogger(__name__)
_SYSTEM = ActorType.SYSTEM
_locks: dict[str, asyncio.Lock] = {}  # serialises advances per simulation within this process


class AdvanceIncompleteError(AppError):
    status_code = 409
    code = "advance_incomplete"


class SimulationResetError(Exception):
    pass


@dataclass
class EventContext:
    session: AsyncSession
    sim: SimulationState
    event: SimulationEvent
    settings: Settings
    counters: Counter[str] = field(default_factory=Counter)

    @property
    def at(self) -> datetime:
        return self.event.scheduled_at_virtual

    async def schedule(
        self,
        event_type: EventType,
        aggregate_type: str,
        aggregate_id: str,
        discriminator: str,
        scheduled_at: datetime,
        payload: dict[str, object],
    ) -> None:
        created = await schedule_event(
            self.session,
            simulation_id=self.sim.simulation_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            discriminator=discriminator,
            scheduled_at=scheduled_at,
            payload=payload,
            not_before=self.at,
        )
        if created is not None:
            self.counters["events_created"] += 1


# --- Handlers: return a short outcome string; "skipped:*" marks a no-op ----------------------


async def _installment(ctx: EventContext) -> BillingInstallment:
    installment = await ctx.session.get(
        BillingInstallment, str(ctx.event.payload["installment_id"])
    )
    if installment is None:
        raise LookupError("installment referenced by event does not exist")
    return installment


async def _pick_scenario(ctx: EventContext, subscription_id: str, period: str) -> str:
    rows = await ctx.session.scalars(
        select(ScenarioAssignment).where(
            ScenarioAssignment.simulation_id == ctx.sim.simulation_id,
            ScenarioAssignment.subscription_id == subscription_id,
            ScenarioAssignment.billing_period.in_([period, ALL_PERIODS]),
        )
    )
    by_period = {a.billing_period: a.scenario_id for a in rows}
    return (
        by_period.get(period)
        or by_period.get(ALL_PERIODS)
        or seeded_scenario_id(ctx.sim.seed, subscription_id, period)
    )


async def on_installment_due(ctx: EventContext) -> str:
    payload = ctx.event.payload
    subscription = await ctx.session.get(Subscription, str(payload["subscription_id"]))
    if subscription is None or not subscription.is_active:
        return "skipped:subscription_inactive"
    period = str(payload["billing_period"])
    key = installment_key(ctx.sim.simulation_id, subscription.subscription_id, period)
    if await ctx.session.scalar(
        select(BillingInstallment.installment_id).where(BillingInstallment.idempotency_key == key)
    ):
        return "skipped:installment_exists"
    installment = BillingInstallment(
        simulation_id=ctx.sim.simulation_id,
        subscription_id=subscription.subscription_id,
        customer_id=subscription.customer_id,
        billing_period=period,
        due_at=ctx.at,
        amount_minor_units=subscription.amount_minor_units,
        currency=subscription.currency,
        payment_status=I.SCHEDULED,
        scenario_id=await _pick_scenario(ctx, subscription.subscription_id, period),
        idempotency_key=key,
    )
    ctx.session.add(installment)
    await ctx.session.flush()
    ctx.counters["installments_created"] += 1
    tz = ZoneInfo(ctx.sim.timezone)
    attempt_at = local_instant(
        ctx.at.astimezone(tz).date(), ctx.settings.simulation_autopay_time, tz
    )
    await ctx.schedule(
        EventType.PAYMENT_ATTEMPT_REQUESTED,
        "installment",
        installment.installment_id,
        "attempt-1",
        max(attempt_at, ctx.at),
        {"installment_id": installment.installment_id, "attempt_number": 1},
    )
    return f"installment_created:{installment.scenario_id}"


_OUTCOME_EVENTS = {
    AttemptOutcome.SUCCEEDED: EventType.PAYMENT_SUCCEEDED,
    AttemptOutcome.ALREADY_PAID: EventType.PAYMENT_SUCCEEDED,
    AttemptOutcome.FAILED: EventType.PAYMENT_FAILED,
    AttemptOutcome.UNKNOWN: EventType.PAYMENT_RECONCILIATION_REQUIRED,
}


async def on_attempt_requested(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    number = int(ctx.event.payload["attempt_number"])
    if installment.payment_status not in (I.SCHEDULED, I.RETRY_SCHEDULED):
        return f"skipped:installment_{installment.payment_status.value}"
    key = attempt_key(installment.installment_id, number)
    if await ctx.session.scalar(
        select(PaymentAttempt.payment_attempt_id).where(PaymentAttempt.idempotency_key == key)
    ):
        return "skipped:attempt_exists"
    scenario = CATALOG[installment.scenario_id]
    outcome = scenario.outcome_for_attempt(number)
    installment.payment_status = check_transition(installment.payment_status, I.PROCESSING)
    ctx.session.add(
        PaymentAttempt(
            installment_id=installment.installment_id,
            attempt_number=number,
            outcome=outcome,
            failure_reason=scenario.failure_reason
            if outcome in (AttemptOutcome.FAILED, AttemptOutcome.UNKNOWN)
            else None,
            attempted_at_virtual=ctx.at,
            scenario_id=scenario.scenario_id,
            idempotency_key=key,
        )
    )
    ctx.counters["payment_attempts_created"] += 1
    source = (
        PaymentSource.ALREADY_PAID_CONFIRMATION
        if outcome == AttemptOutcome.ALREADY_PAID
        else PaymentSource.AUTOPAY
    )
    await ctx.schedule(
        _OUTCOME_EVENTS[outcome],
        "installment",
        installment.installment_id,
        f"attempt-{number}",
        ctx.at,
        {
            "installment_id": installment.installment_id,
            "attempt_number": number,
            "source": source.value,
        },
    )
    return f"attempt_{number}:{outcome.value}"


async def on_payment_succeeded(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    if installment.payment_status == I.SUCCESS:
        return "skipped:already_success"
    installment.payment_status = check_transition(installment.payment_status, I.SUCCESS)
    source = PaymentSource(str(ctx.event.payload.get("source", PaymentSource.AUTOPAY.value)))
    installment.payment_source = source
    installment.paid_at_virtual = ctx.at
    case = await recovery.end_case_for_payment(
        ctx.session,
        installment,
        already_paid=source == PaymentSource.ALREADY_PAID_CONFIRMATION,
        virtual_time=ctx.at,
    )
    if case is not None:
        ctx.counters["recovery_cases_ended"] += 1
    return f"success:{source.value}"


async def on_payment_failed(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    number = int(ctx.event.payload["attempt_number"])
    if installment.payment_status not in (I.PROCESSING, I.PENDING_RECONCILIATION):
        return f"skipped:installment_{installment.payment_status.value}"
    installment.payment_status = check_transition(installment.payment_status, I.FAILED)
    scenario = CATALOG[installment.scenario_id]
    result = "failed:final"
    if scenario.retry_permitted and number < scenario.max_attempts:
        installment.payment_status = check_transition(I.FAILED, I.RETRY_SCHEDULED)
        await ctx.schedule(
            EventType.RETRY_DUE,
            "installment",
            installment.installment_id,
            f"attempt-{number + 1}",
            ctx.at + scenario.retry_delay,
            {"installment_id": installment.installment_id, "attempt_number": number + 1},
        )
        result = f"failed:retry_{number + 1}_scheduled"
    case = await recovery.open_case(
        ctx.session, installment, failed_at=ctx.at, eligible_at=ctx.at + scenario.recovery_delay
    )
    if case is not None:
        ctx.counters["recovery_cases_created"] += 1
        await ctx.schedule(
            EventType.RECOVERY_ELIGIBLE,
            "recovery_case",
            case.recovery_case_id,
            "eligible",
            case.eligible_at,
            {"recovery_case_id": case.recovery_case_id},
        )
    return result


async def on_retry_due(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    if installment.payment_status != I.RETRY_SCHEDULED:
        return f"skipped:installment_{installment.payment_status.value}"
    number = int(ctx.event.payload["attempt_number"])
    await ctx.schedule(
        EventType.PAYMENT_ATTEMPT_REQUESTED,
        "installment",
        installment.installment_id,
        f"attempt-{number}",
        ctx.at,
        {"installment_id": installment.installment_id, "attempt_number": number},
    )
    return f"retry_attempt_{number}_requested"


async def on_reconciliation_required(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    if installment.payment_status != I.PROCESSING:
        return f"skipped:installment_{installment.payment_status.value}"
    installment.payment_status = check_transition(I.PROCESSING, I.PENDING_RECONCILIATION)
    scenario = CATALOG[installment.scenario_id]
    if scenario.reconciliation_delay is None:
        return "pending_reconciliation:manual"  # stays pending until a trusted result arrives
    number = int(ctx.event.payload["attempt_number"])
    await ctx.schedule(
        EventType.PAYMENT_RECONCILIATION_RESOLVED,
        "installment",
        installment.installment_id,
        f"attempt-{number}",
        ctx.at + scenario.reconciliation_delay,
        {"installment_id": installment.installment_id, "attempt_number": number},
    )
    return "pending_reconciliation"


async def on_reconciliation_resolved(ctx: EventContext) -> str:
    installment = await _installment(ctx)
    if installment.payment_status != I.PENDING_RECONCILIATION:
        return f"skipped:installment_{installment.payment_status.value}"
    number = int(ctx.event.payload["attempt_number"])
    scenario = CATALOG[installment.scenario_id]
    outcome = scenario.reconciled_outcome or AttemptOutcome.FAILED
    attempt = await ctx.session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.idempotency_key == attempt_key(installment.installment_id, number)
        )
    )
    if attempt is not None and attempt.outcome == AttemptOutcome.UNKNOWN:
        attempt.outcome = outcome
        attempt.reconciled_at_virtual = ctx.at
    await ctx.schedule(
        _OUTCOME_EVENTS[outcome],
        "installment",
        installment.installment_id,
        f"reconciled-{number}",
        ctx.at,
        {
            "installment_id": installment.installment_id,
            "attempt_number": number,
            "source": PaymentSource.RECONCILIATION.value,
        },
    )
    return f"reconciled:{outcome.value}"


async def _case(ctx: EventContext) -> RecoveryCase:
    case = await ctx.session.get(RecoveryCase, str(ctx.event.payload["recovery_case_id"]))
    if case is None:
        raise LookupError("recovery case referenced by event does not exist")
    return case


async def on_recovery_eligible(ctx: EventContext) -> str:
    case = await _case(ctx)
    if case.status != R.OPEN:
        return f"skipped:case_{case.status.value}"
    recovery.transition(ctx.session, case, R.ELIGIBLE, actor_type=_SYSTEM)
    await ctx.schedule(
        EventType.RECOVERY_APPROVAL_REQUIRED,
        "recovery_case",
        case.recovery_case_id,
        "approval",
        ctx.at,
        {"recovery_case_id": case.recovery_case_id},
    )
    return "eligible"


async def on_approval_required(ctx: EventContext) -> str:
    """Queue for a human. Never places a call."""
    case = await _case(ctx)
    if case.status != R.ELIGIBLE:
        return f"skipped:case_{case.status.value}"
    customer = await ctx.session.get(Customer, case.customer_id)
    if customer is None or customer.do_not_contact:
        recovery.opt_out(ctx.session, case, virtual_time=ctx.at, actor_type=_SYSTEM)
        return "opted_out"
    recovery.transition(ctx.session, case, R.AWAITING_APPROVAL, actor_type=_SYSTEM)
    case.approval_requested_at = ctx.at
    ctx.counters["recovery_cases_queued"] += 1
    return "awaiting_approval"


HANDLERS = {
    EventType.INSTALLMENT_DUE: on_installment_due,
    EventType.PAYMENT_ATTEMPT_REQUESTED: on_attempt_requested,
    EventType.PAYMENT_SUCCEEDED: on_payment_succeeded,
    EventType.PAYMENT_FAILED: on_payment_failed,
    EventType.RETRY_DUE: on_retry_due,
    EventType.PAYMENT_RECONCILIATION_REQUIRED: on_reconciliation_required,
    EventType.PAYMENT_RECONCILIATION_RESOLVED: on_reconciliation_resolved,
    EventType.RECOVERY_ELIGIBLE: on_recovery_eligible,
    EventType.RECOVERY_APPROVAL_REQUIRED: on_approval_required,
}


# --- Advance loop ---------------------------------------------------------------------------


@dataclass
class AdvanceResult:
    advance: SimulationAdvance
    replayed: bool = False


async def _materialise_due_events(
    session: AsyncSession, sim: SimulationState, target: datetime
) -> int:
    tz = ZoneInfo(sim.timezone)
    start, end = sim.initial_virtual_time.astimezone(tz).date(), target.astimezone(tz).date()
    created = 0
    subscriptions = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.is_active.is_(True))
            .order_by(Subscription.subscription_id)
        )
    ).all()
    for subscription in subscriptions:
        for period, due in due_dates(
            subscription.billing_frequency, subscription.billing_anchor_date, start, end
        ):
            due_at = local_instant(due, datetime.min.time(), tz)
            if not sim.initial_virtual_time <= due_at <= target:
                continue
            event = await schedule_event(
                session,
                simulation_id=sim.simulation_id,
                event_type=EventType.INSTALLMENT_DUE,
                aggregate_type="subscription",
                aggregate_id=subscription.subscription_id,
                discriminator=period,
                scheduled_at=due_at,
                payload={"subscription_id": subscription.subscription_id, "billing_period": period},
            )
            created += event is not None
    return created


async def _next_event(
    session: AsyncSession, simulation_id: str, target: datetime
) -> SimulationEvent | None:
    return await session.scalar(
        select(SimulationEvent)
        .where(
            SimulationEvent.simulation_id == simulation_id,
            SimulationEvent.status.in_([EventStatus.PENDING, EventStatus.FAILED]),
            SimulationEvent.scheduled_at_virtual <= target,
        )
        .order_by(SimulationEvent.scheduled_at_virtual, SimulationEvent.sequence)
        .limit(1)
    )


class EventProcessor:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    async def advance(
        self, simulation_id: str, target: datetime, *, idempotency_key: str | None = None
    ) -> AdvanceResult:
        lock = _locks.setdefault(simulation_id, asyncio.Lock())
        async with lock:
            return await self._advance(simulation_id, target, idempotency_key)

    async def _start(
        self, simulation_id: str, target: datetime, idempotency_key: str | None
    ) -> AdvanceResult | str:
        async with self.session_factory() as session, session.begin():
            sim = await session.get(SimulationState, simulation_id)
            if sim is None:
                raise NotFoundError(f"Simulation {simulation_id} not found")
            if sim.status != SimulationStatus.ACTIVE:
                raise ConflictError("Simulation is paused")
            if idempotency_key:
                previous = await session.scalar(
                    select(SimulationAdvance).where(
                        SimulationAdvance.simulation_id == simulation_id,
                        SimulationAdvance.idempotency_key == idempotency_key,
                    )
                )
                if previous is not None:
                    if previous.target_time != target or previous.generation != sim.generation:
                        raise ConflictError("Idempotency key reused for a different advance")
                    if previous.status == AdvanceStatus.COMPLETED:
                        return AdvanceResult(previous, replayed=True)
                    previous.status = AdvanceStatus.IN_PROGRESS  # resume
                    return previous.id
            if target < sim.current_virtual_time:
                raise ConflictError(
                    "The virtual clock cannot move backwards; use an explicit reset"
                )
            await session.execute(
                update(SimulationAdvance)
                .where(
                    SimulationAdvance.simulation_id == simulation_id,
                    SimulationAdvance.status.in_([AdvanceStatus.IN_PROGRESS, AdvanceStatus.FAILED]),
                )
                .values(status=AdvanceStatus.INTERRUPTED)
            )
            run = SimulationAdvance(
                simulation_id=simulation_id,
                idempotency_key=idempotency_key,
                generation=sim.generation,
                previous_time=sim.current_virtual_time,
                target_time=target,
                status=AdvanceStatus.IN_PROGRESS,
                counters={},
            )
            session.add(run)
            created = await _materialise_due_events(session, sim, target)
            run.counters = {"events_created": created}
            await session.flush()
            return run.id

    async def _advance(
        self, simulation_id: str, target: datetime, idempotency_key: str | None
    ) -> AdvanceResult:
        started = await self._start(simulation_id, target, idempotency_key)
        if isinstance(started, AdvanceResult):
            return started
        run_id = started
        wall = time.perf_counter()
        processed = 0
        while True:
            if processed >= self.settings.simulation_max_events_per_advance:
                await self._fail_run(run_id, "event limit per advance exceeded (possible cycle)")
                raise AdvanceIncompleteError(
                    "Too many events in one advance; possible scheduling cycle",
                    details={"events_processed": processed},
                )
            outcome = await self._process_next(run_id, simulation_id, target)
            if outcome == "done":
                break
            processed += 1
        async with self.session_factory() as session:
            run = await session.get(SimulationAdvance, run_id)
            assert run is not None  # noqa: S101  created above
            logger.info(
                "simulation advanced",
                extra={
                    "simulation_id": simulation_id,
                    "counters": run.counters,
                    "wall_ms": round((time.perf_counter() - wall) * 1000, 1),
                },
            )
            return AdvanceResult(run)

    async def _process_next(self, run_id: str, simulation_id: str, target: datetime) -> str:
        async with self.session_factory() as session:
            event = await _next_event(session, simulation_id, target)
            if event is not None and event.status == EventStatus.FAILED:
                # Read attributes before rollback(): rollback expires loaded objects.
                details = {
                    "event_id": event.event_id,
                    "event_type": event.event_type.value,
                    "last_error": event.last_error,
                }
                await session.rollback()
                await self._block_run(run_id, str(details["event_id"]))
                raise AdvanceIncompleteError(
                    "A dead-lettered event blocks the clock; retry it before advancing",
                    details=details,
                )
            if event is None:
                await session.rollback()
                await self._complete_run(session, run_id, simulation_id, target)
                return "done"
            event_id = event.event_id
            await session.rollback()
            try:
                async with session.begin():
                    # Claim first: the UPDATE takes SQLite's write lock for this transaction.
                    claimed = await session.execute(
                        update(SimulationEvent)
                        .where(
                            SimulationEvent.event_id == event_id,
                            SimulationEvent.status == EventStatus.PENDING,
                        )
                        .values(attempt_count=SimulationEvent.attempt_count + 1)
                    )
                    if claimed.rowcount != 1:  # type: ignore[attr-defined]
                        return "raced"
                    event = await session.get(SimulationEvent, event_id, populate_existing=True)
                    sim = await session.get(SimulationState, simulation_id)
                    run = await session.get(SimulationAdvance, run_id)
                    if (
                        event is None
                        or sim is None
                        or run is None
                        or (sim.generation != run.generation)
                    ):
                        raise SimulationResetError("simulation was reset during the advance")
                    ctx = EventContext(session, sim, event, self.settings)
                    outcome = await HANDLERS[event.event_type](ctx)
                    event.status = (
                        EventStatus.SKIPPED
                        if outcome.startswith("skipped")
                        else EventStatus.PROCESSED
                    )
                    event.outcome = outcome[:200]
                    event.last_error = None
                    event.processed_at_virtual = event.scheduled_at_virtual
                    event.processed_at_real = utc_now()
                    if event.scheduled_at_virtual > sim.current_virtual_time:
                        sim.current_virtual_time = event.scheduled_at_virtual
                    counters = Counter(run.counters) + ctx.counters
                    counters["events_processed"] += 1
                    counters["skipped" if event.status == EventStatus.SKIPPED else "applied"] += 1
                    run.counters = dict(counters)
                    run.final_time = sim.current_virtual_time
            except SimulationResetError:
                raise ConflictError("The simulation was reset during the advance") from None
            except Exception as exc:
                logger.exception("event processing failed", extra={"event_id": event_id})
                await self._record_failure(event_id, run_id, exc)
                raise AdvanceIncompleteError(
                    "An event failed; the clock stopped at the last fully processed event. "
                    "Re-issue the advance to retry.",
                    details={"event_id": event_id, "error": type(exc).__name__},
                ) from exc
            return "processed"

    async def _record_failure(self, event_id: str, run_id: str, exc: Exception) -> None:
        async with self.session_factory() as session, session.begin():
            event = await session.get(SimulationEvent, event_id)
            run = await session.get(SimulationAdvance, run_id)
            if event is None or run is None:
                return
            event.attempt_count += 1
            event.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if event.attempt_count >= self.settings.simulation_max_event_attempts:
                event.status = EventStatus.FAILED
            run.status = AdvanceStatus.FAILED
            run.error = event.last_error

    async def _block_run(self, run_id: str, event_id: str) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.get(SimulationAdvance, run_id)
            if run is not None:
                run.status = AdvanceStatus.BLOCKED
                run.error = f"blocked by failed event {event_id}"

    async def _fail_run(self, run_id: str, message: str) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.get(SimulationAdvance, run_id)
            if run is not None:
                run.status, run.error = AdvanceStatus.FAILED, message

    async def _complete_run(
        self, session: AsyncSession, run_id: str, simulation_id: str, target: datetime
    ) -> None:
        async with session.begin():
            sim = await session.get(SimulationState, simulation_id)
            run = await session.get(SimulationAdvance, run_id)
            if sim is None or run is None or sim.generation != run.generation:
                raise ConflictError("The simulation was reset during the advance")
            if target > sim.current_virtual_time:
                sim.current_virtual_time = target
            run.final_time = sim.current_virtual_time
            run.status = AdvanceStatus.COMPLETED
            run.completed_at = utc_now()
