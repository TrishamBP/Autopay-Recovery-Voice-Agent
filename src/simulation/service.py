"""Simulation operations other than advancing (which lives in the processor)."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.service import record_audit
from src.billing.models import BillingInstallment, PaymentAttempt, Subscription
from src.billing.state_machine import InstallmentStatus as I
from src.billing.state_machine import PaymentSource
from src.calls.models import CallSession
from src.common.enums import ActorType
from src.common.exceptions import ConflictError, NotFoundError, ValidationFailedError
from src.config import Settings
from src.recovery.models import RecoveryCase
from src.simulation.events import EventStatus, EventType, SimulationStatus
from src.simulation.ledger import record_fact, schedule_event
from src.simulation.models import (
    ALL_PERIODS,
    ScenarioAssignment,
    SimulationAdvance,
    SimulationEvent,
    SimulationState,
)
from src.simulation.processor import EventProcessor
from src.simulation.scenarios import CATALOG

DEFAULT_SEED = 20261001


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailedError(f"Unknown IANA time zone {name!r}") from exc


def _scenario(scenario_id: str) -> str:
    if scenario_id not in CATALOG:
        raise ValidationFailedError(
            f"Unknown scenario {scenario_id!r}", details={"known": sorted(CATALOG)}
        )
    return scenario_id


async def get_simulation(session: AsyncSession, simulation_id: str) -> SimulationState:
    sim = await session.get(SimulationState, simulation_id)
    if sim is None:
        raise NotFoundError(f"Simulation {simulation_id} not found")
    return sim


async def create_simulation(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    simulation_id: str,
    initial_time: datetime | None,
    timezone: str | None,
    seed: int | None,
    assignments: Sequence[tuple[str, str, str]] = (),
) -> tuple[SimulationState, bool]:
    """Create (idempotently). Returns (state, created). An existing simulation with the same
    configuration is returned as-is; a different configuration is a conflict."""
    tz_name = timezone or settings.simulation_default_timezone
    _zone(tz_name)
    start = initial_time or settings.simulation_initial_time
    if start.tzinfo is None:
        raise ValidationFailedError("initial_time must include a UTC offset")
    async with factory() as session, session.begin():
        existing = await session.get(SimulationState, simulation_id)
        if existing is not None:
            same = (
                existing.initial_virtual_time == start
                and existing.timezone == tz_name
                and existing.seed == (seed if seed is not None else existing.seed)
            )
            if not same:
                raise ConflictError("A simulation with this id exists with different settings")
            return existing, False
        sim = SimulationState(
            simulation_id=simulation_id,
            current_virtual_time=start,
            initial_virtual_time=start,
            timezone=tz_name,
            status=SimulationStatus.ACTIVE,
            seed=seed if seed is not None else DEFAULT_SEED,
            configuration_version=1,
            generation=1,
        )
        session.add(sim)
        await session.flush()
        for subscription_id, period, scenario_id in assignments:
            await _assign(session, sim, subscription_id, period, scenario_id)
        record_audit(
            session,
            event_type="simulation.created",
            entity_type="simulation",
            entity_id=simulation_id,
            actor_type=ActorType.OPERATOR,
            details={"initial_time": start.isoformat(), "timezone": tz_name},
        )
        return sim, True


async def _assign(
    session: AsyncSession, sim: SimulationState, subscription_id: str, period: str, scenario_id: str
) -> ScenarioAssignment:
    _scenario(scenario_id)
    if await session.get(Subscription, subscription_id) is None:
        raise NotFoundError(f"Subscription {subscription_id} not found")
    if period != ALL_PERIODS:
        installment = await session.scalar(
            select(BillingInstallment).where(
                BillingInstallment.simulation_id == sim.simulation_id,
                BillingInstallment.subscription_id == subscription_id,
                BillingInstallment.billing_period == period,
            )
        )
        if installment is not None:
            raise ConflictError(
                "That installment already exists; use inject-scenario on it",
                details={"installment_id": installment.installment_id},
            )
    assignment = await session.scalar(
        select(ScenarioAssignment).where(
            ScenarioAssignment.simulation_id == sim.simulation_id,
            ScenarioAssignment.subscription_id == subscription_id,
            ScenarioAssignment.billing_period == period,
        )
    )
    if assignment is None:
        assignment = ScenarioAssignment(
            simulation_id=sim.simulation_id,
            subscription_id=subscription_id,
            billing_period=period,
            scenario_id=scenario_id,
        )
        session.add(assignment)
    else:
        assignment.scenario_id = scenario_id
    sim.configuration_version += 1
    return assignment


async def assign_scenario(
    factory: async_sessionmaker[AsyncSession],
    *,
    simulation_id: str,
    subscription_id: str,
    billing_period: str | None,
    scenario_id: str,
) -> ScenarioAssignment:
    async with factory() as session, session.begin():
        sim = await get_simulation(session, simulation_id)
        assignment = await _assign(
            session, sim, subscription_id, billing_period or ALL_PERIODS, scenario_id
        )
        record_audit(
            session,
            event_type="scenario.assigned",
            entity_type="subscription",
            entity_id=subscription_id,
            actor_type=ActorType.OPERATOR,
            details={
                "simulation_id": simulation_id,
                "billing_period": billing_period,
                "scenario_id": scenario_id,
            },
        )
        return assignment


async def reset_simulation(
    factory: async_sessionmaker[AsyncSession],
    simulation_id: str,
    *,
    initial_time: datetime | None = None,
) -> SimulationState:
    """Development operation: delete this simulation's billing/recovery/event data and put the
    clock back to its initial time. Scenario assignments (configuration) are kept. Call sessions
    are kept for audit but detached. `generation` increments so in-flight work is discarded."""
    async with factory() as session, session.begin():
        sim = await get_simulation(session, simulation_id)
        installments = select(BillingInstallment.installment_id).where(
            BillingInstallment.simulation_id == simulation_id
        )
        await session.execute(
            delete(PaymentAttempt).where(PaymentAttempt.installment_id.in_(installments))
        )
        await session.execute(
            update(CallSession)
            .where(CallSession.simulation_id == simulation_id)
            .values(recovery_case_id=None)
        )
        for model in (RecoveryCase, BillingInstallment, SimulationEvent, SimulationAdvance):
            await session.execute(delete(model).where(model.simulation_id == simulation_id))
        if initial_time is not None:
            if initial_time.tzinfo is None:
                raise ValidationFailedError("initial_time must include a UTC offset")
            sim.initial_virtual_time = initial_time
        sim.current_virtual_time = sim.initial_virtual_time
        sim.generation += 1
        sim.status = SimulationStatus.ACTIVE
        record_audit(
            session,
            event_type="simulation.reset",
            entity_type="simulation",
            entity_id=simulation_id,
            actor_type=ActorType.OPERATOR,
            details={
                "generation": sim.generation,
                "initial_time": sim.initial_virtual_time.isoformat(),
            },
        )
        return sim


async def find_advance(
    session: AsyncSession, simulation_id: str, idempotency_key: str
) -> SimulationAdvance | None:
    return await session.scalar(
        select(SimulationAdvance).where(
            SimulationAdvance.simulation_id == simulation_id,
            SimulationAdvance.idempotency_key == idempotency_key,
        )
    )


async def list_events(
    session: AsyncSession,
    *,
    simulation_id: str,
    status: EventStatus | None,
    event_type: EventType | None,
    since: datetime | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[SimulationEvent], int]:
    conditions = [SimulationEvent.simulation_id == simulation_id]
    if status is not None:
        conditions.append(SimulationEvent.status == status)
    if event_type is not None:
        conditions.append(SimulationEvent.event_type == event_type)
    if since is not None:
        conditions.append(SimulationEvent.scheduled_at_virtual >= since)
    total = await session.scalar(
        select(func.count()).select_from(SimulationEvent).where(*conditions)
    )
    rows = await session.scalars(
        select(SimulationEvent)
        .where(*conditions)
        .order_by(SimulationEvent.scheduled_at_virtual, SimulationEvent.sequence)
        .limit(limit)
        .offset(offset)
    )
    return rows.all(), total or 0


async def retry_event(factory: async_sessionmaker[AsyncSession], event_id: str) -> SimulationEvent:
    async with factory() as session, session.begin():
        event = await session.get(SimulationEvent, event_id)
        if event is None:
            raise NotFoundError("Event not found")
        if event.status != EventStatus.FAILED:
            raise ConflictError("Only dead-lettered (failed) events can be retried")
        event.status, event.attempt_count = EventStatus.PENDING, 0
        record_audit(
            session,
            event_type="simulation_event.retried",
            entity_type="simulation_event",
            entity_id=event_id,
            actor_type=ActorType.OPERATOR,
        )
        return event


async def inject_scenario(
    factory: async_sessionmaker[AsyncSession],
    processor: EventProcessor,
    *,
    installment_id: str,
    scenario_id: str,
    reason: str,
) -> BillingInstallment:
    """Explicit test operation. Finalised installments cannot be changed (reset instead).

    - ALREADY_PAID on an unpaid installment schedules a trusted confirmation at the current
      virtual time and processes it immediately (clock unchanged).
    - Any scenario on SCHEDULED / RETRY_SCHEDULED changes the outcome of FUTURE attempts only;
      recorded attempts are never rewritten.
    """
    _scenario(scenario_id)
    async with factory() as session, session.begin():
        installment = await session.get(BillingInstallment, installment_id)
        if installment is None:
            raise NotFoundError("Installment not found")
        sim = await get_simulation(session, installment.simulation_id)
        status = installment.payment_status
        if status == I.SUCCESS:
            raise ConflictError(
                "The installment is finalised (paid); reset the simulation to change it"
            )
        if status == I.PROCESSING:
            raise ConflictError("The installment is mid-attempt; advance the clock first")
        if scenario_id == "ALREADY_PAID" and status in (
            I.FAILED,
            I.RETRY_SCHEDULED,
            I.PENDING_RECONCILIATION,
        ):
            await schedule_event(
                session,
                simulation_id=sim.simulation_id,
                event_type=EventType.PAYMENT_SUCCEEDED,
                aggregate_type="installment",
                aggregate_id=installment_id,
                discriminator=f"already-paid-{sim.configuration_version}",
                scheduled_at=sim.current_virtual_time,
                payload={
                    "installment_id": installment_id,
                    "source": PaymentSource.ALREADY_PAID_CONFIRMATION.value,
                },
            )
        elif status in (I.SCHEDULED, I.RETRY_SCHEDULED):
            installment.scenario_id = scenario_id
        else:
            raise ConflictError(
                "No further attempts are scheduled; only an ALREADY_PAID confirmation applies"
            )
        sim.configuration_version += 1
        await record_fact(
            session,
            simulation_id=sim.simulation_id,
            event_type=EventType.SCENARIO_INJECTED,
            aggregate_type="installment",
            aggregate_id=installment_id,
            discriminator=f"v{sim.configuration_version}",
            virtual_time=sim.current_virtual_time,
            payload={"scenario_id": scenario_id, "reason": reason},
        )
        record_audit(
            session,
            event_type="installment.scenario_injected",
            entity_type="billing_installment",
            entity_id=installment_id,
            actor_type=ActorType.OPERATOR,
            details={"scenario_id": scenario_id, "reason": reason},
        )
        simulation_id, now = sim.simulation_id, sim.current_virtual_time
    await processor.advance(simulation_id, now)  # drain events due now; the clock does not move
    async with factory() as session:
        refreshed = await session.get(BillingInstallment, installment_id)
        if refreshed is None:
            raise NotFoundError("Installment not found")
        return refreshed


async def summary(session: AsyncSession, simulation_id: str) -> dict[str, object]:
    sim = await get_simulation(session, simulation_id)

    async def group(column: object, model: type, where: object) -> dict[str, int]:
        rows = await session.execute(
            select(column, func.count())
            .select_from(model)  # type: ignore[call-overload]
            .where(where)
            .group_by(column)
        )
        return {str(getattr(k, "value", k)): n for k, n in rows.all()}

    attempts = await session.execute(
        select(PaymentAttempt.outcome, func.count())
        .join(
            BillingInstallment, BillingInstallment.installment_id == PaymentAttempt.installment_id
        )
        .where(BillingInstallment.simulation_id == simulation_id)
        .group_by(PaymentAttempt.outcome)
    )
    calls = await session.scalar(
        select(func.count())
        .select_from(CallSession)
        .where(CallSession.simulation_id == simulation_id)
    )
    tz = ZoneInfo(sim.timezone)
    return {
        "simulation_id": simulation_id,
        "current_time": sim.current_virtual_time.astimezone(tz).isoformat(),
        "installments_by_status": await group(
            BillingInstallment.payment_status,
            BillingInstallment,
            BillingInstallment.simulation_id == simulation_id,
        ),
        "payment_attempts_by_outcome": {o.value: n for o, n in attempts.all()},
        "recovery_cases_by_status": await group(
            RecoveryCase.status, RecoveryCase, RecoveryCase.simulation_id == simulation_id
        ),
        "events_by_status": await group(
            SimulationEvent.status, SimulationEvent, SimulationEvent.simulation_id == simulation_id
        ),
        "calls_initiated": calls or 0,
    }


def advance_target(
    sim: SimulationState,
    *,
    advance_days: int | None,
    target_time: datetime | None,
    settings: Settings,
) -> datetime:
    if (advance_days is None) == (target_time is None):
        raise ValidationFailedError("Provide exactly one of advance_days or target_time")
    if advance_days is not None:
        if advance_days > settings.simulation_max_advance_days:
            raise ValidationFailedError(
                f"advance_days must be <= {settings.simulation_max_advance_days}"
            )
        # Calendar days in the simulation time zone (wall-clock arithmetic, DST-safe).
        tz = ZoneInfo(sim.timezone)
        local = sim.current_virtual_time.astimezone(tz)
        return (
            (local.replace(tzinfo=None) + timedelta(days=advance_days))
            .replace(tzinfo=tz)
            .astimezone(sim.current_virtual_time.tzinfo)
        )
    assert target_time is not None  # noqa: S101  checked above
    if target_time.tzinfo is None:
        raise ValidationFailedError("target_time must include a UTC offset")
    return target_time
