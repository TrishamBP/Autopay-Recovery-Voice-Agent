"""Virtual clock, deterministic scenarios, idempotency and failure recovery (service level)."""

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.billing.models import BillingInstallment, PaymentAttempt
from src.billing.state_machine import InstallmentStatus as I
from src.calls.models import CallSession
from src.common.exceptions import ConflictError
from src.config import Settings
from src.recovery import service as recovery_service
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.simulation import processor as processor_module
from src.simulation import service
from src.simulation.events import EventStatus, EventType
from src.simulation.models import SimulationEvent, SimulationState
from src.simulation.processor import AdvanceIncompleteError, EventProcessor
from tests.sim_fixtures import START, Spec, ist, seed_customers, three_customers

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def sim(session_factory: Factory, settings: Settings) -> EventProcessor:
    await seed_customers(session_factory, three_customers())
    return EventProcessor(session_factory, settings)


async def create(
    factory: Factory,
    settings: Settings,
    sim_id: str = "demo-001",
    assignments: dict[str, str] | None = None,
    **kw: object,
) -> None:
    await service.create_simulation(
        factory,
        settings,
        simulation_id=sim_id,
        initial_time=kw.get("initial_time", START),
        timezone=None,
        seed=kw.get("seed"),  # type: ignore[arg-type]
        assignments=[(sub, "*", scenario) for sub, scenario in (assignments or {}).items()],
    )


async def rows(factory: Factory, model: type, *where: object) -> list:
    async with factory() as s:
        return list((await s.scalars(select(model).where(*where))).all())


async def count(factory: Factory, model: type, *where: object) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where)) or 0


ALL_SUCCESS = {
    "SUB-3001": "SUCCESSFUL_PAYMENT",
    "SUB-3002": "SUCCESSFUL_PAYMENT",
    "SUB-3003": "SUCCESSFUL_PAYMENT",
}


# --- Virtual clock -------------------------------------------------------------------------


async def test_clock_is_persisted_and_rejects_backwards(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, assignments=ALL_SUCCESS)
    result = await sim.advance("demo-001", ist(2026, 10, 3))
    assert result.advance.final_time == ist(2026, 10, 3)
    async with session_factory() as s:  # fresh session: read from the database, not memory
        state = await s.get(SimulationState, "demo-001")
        assert state is not None and state.current_virtual_time == ist(2026, 10, 3)
    with pytest.raises(ConflictError, match="backwards"):
        await sim.advance("demo-001", ist(2026, 10, 2))


async def test_multi_month_advance_processes_every_event_in_order(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, assignments=ALL_SUCCESS)
    await sim.advance("demo-001", ist(2027, 1, 31, 23))
    installments = await rows(session_factory, BillingInstallment)
    assert len(installments) == 12  # 3 subscriptions x Oct, Nov, Dec, Jan
    assert {i.payment_status for i in installments} == {I.SUCCESS}
    events = sorted(
        await rows(
            session_factory, SimulationEvent, SimulationEvent.status == EventStatus.PROCESSED
        ),
        key=lambda e: e.processed_at_real,
    )
    times = [(e.scheduled_at_virtual, e.sequence) for e in events]
    assert times == sorted(times)  # processed strictly in virtual chronological order


async def test_single_large_advance_equals_many_small_ones(
    session_factory: Factory, settings: Settings, sim: EventProcessor
) -> None:
    mix = {
        "SUB-3001": "INSUFFICIENT_FUNDS",
        "SUB-3002": "BANK_TIMEOUT",
        "SUB-3003": "INSUFFICIENT_FUNDS_RECOVERS",
    }
    await create(session_factory, settings, "big", assignments=mix)
    await create(session_factory, settings, "small", assignments=mix)
    await sim.advance("big", ist(2026, 11, 20))
    day = START
    while day < ist(2026, 11, 20):
        day = day.replace(day=day.day + 1) if day.day < 28 else ist(day.year, day.month + 1, 1)
        await sim.advance("small", min(day, ist(2026, 11, 20)))

    async def snapshot(sim_id: str) -> list[tuple]:
        insts = await rows(
            session_factory, BillingInstallment, BillingInstallment.simulation_id == sim_id
        )
        cases = await rows(session_factory, RecoveryCase, RecoveryCase.simulation_id == sim_id)
        return sorted(
            (i.subscription_id, i.billing_period, i.payment_status.value) for i in insts
        ) + sorted((c.customer_id, c.status.value, c.eligible_at.isoformat()) for c in cases)

    assert await snapshot("big") == await snapshot("small")


async def test_simulations_are_isolated(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, "a", assignments=ALL_SUCCESS)
    await create(session_factory, settings, "b", assignments=ALL_SUCCESS)
    await sim.advance("a", ist(2026, 10, 10))
    assert (
        await count(session_factory, BillingInstallment, BillingInstallment.simulation_id == "b")
        == 0
    )
    async with session_factory() as s:
        b = await s.get(SimulationState, "b")
        assert b is not None and b.current_virtual_time == START


async def test_reset_restores_initial_state(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "MANDATE_INACTIVE"}
    )
    await sim.advance("demo-001", ist(2026, 10, 10))
    assert await count(session_factory, RecoveryCase) == 1
    state = await service.reset_simulation(session_factory, "demo-001")
    assert state.current_virtual_time == START and state.generation == 2
    for model in (BillingInstallment, PaymentAttempt, RecoveryCase, SimulationEvent):
        assert await count(session_factory, model) == 0
    await sim.advance("demo-001", ist(2026, 10, 10))  # replays identically after reset
    cases = await rows(session_factory, RecoveryCase)
    assert len(cases) == 1 and cases[0].customer_id == "CUS-2001"


# --- Payment scenarios ---------------------------------------------------------------------


async def test_success_creates_no_recovery_case(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, assignments=ALL_SUCCESS)
    await sim.advance("demo-001", ist(2026, 10, 10))
    assert await count(session_factory, RecoveryCase) == 0
    attempts = await rows(session_factory, PaymentAttempt)
    assert {a.outcome.value for a in attempts} == {"succeeded"} and len(attempts) == 3


async def test_definitive_failure_opens_exactly_one_case(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "MANDATE_INACTIVE"}
    )
    await sim.advance("demo-001", ist(2026, 10, 2, 12))  # attempt at 11:00 on the 2nd
    cases = await rows(session_factory, RecoveryCase)
    assert len(cases) == 1
    assert cases[0].status == R.OPEN
    assert cases[0].eligible_at == ist(2026, 10, 3, 11)  # failure time + 1 day policy delay
    await sim.advance("demo-001", ist(2026, 12, 31))
    assert (
        await count(session_factory, RecoveryCase, RecoveryCase.customer_id == "CUS-2001") == 3
    )  # one per failed period


async def test_bank_timeout_blocks_recovery_until_reconciled(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "BANK_TIMEOUT"})
    await sim.advance("demo-001", ist(2026, 10, 4, 10))  # before the 48h reconciliation
    [installment] = await rows(
        session_factory, BillingInstallment, BillingInstallment.customer_id == "CUS-2001"
    )
    assert installment.payment_status == I.PENDING_RECONCILIATION
    assert await count(session_factory, RecoveryCase, RecoveryCase.customer_id == "CUS-2001") == 0
    await sim.advance("demo-001", ist(2026, 10, 4, 12))  # reconciled as failed at 11:00
    [attempt] = await rows(
        session_factory, PaymentAttempt, PaymentAttempt.installment_id == installment.installment_id
    )
    assert attempt.outcome.value == "failed" and attempt.reconciled_at_virtual == ist(
        2026, 10, 4, 11
    )
    assert await count(session_factory, RecoveryCase) == 1


async def test_retries_are_attempts_on_the_same_installment(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "INSUFFICIENT_FUNDS"}
    )
    await sim.advance("demo-001", ist(2026, 10, 10))
    installments = await rows(
        session_factory, BillingInstallment, BillingInstallment.customer_id == "CUS-2001"
    )
    attempts = sorted(
        await rows(
            session_factory,
            PaymentAttempt,
            PaymentAttempt.installment_id == installments[0].installment_id,
        ),
        key=lambda a: a.attempt_number,
    )
    assert len(installments) == 1 and installments[0].payment_status == I.FAILED
    assert [(a.attempt_number, a.attempted_at_virtual) for a in attempts] == [
        (1, ist(2026, 10, 2, 11)),
        (2, ist(2026, 10, 4, 11)),
        (3, ist(2026, 10, 6, 11)),
    ]
    assert await count(session_factory, RecoveryCase) == 1


async def test_successful_retry_cancels_recovery(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory,
        settings,
        assignments=ALL_SUCCESS | {"SUB-3001": "INSUFFICIENT_FUNDS_RECOVERS"},
    )
    await sim.advance("demo-001", ist(2026, 10, 3, 12))  # eligible at 10-03 11:00 -> queued
    [case] = await rows(session_factory, RecoveryCase)
    assert case.status == R.AWAITING_APPROVAL
    await sim.advance("demo-001", ist(2026, 10, 5))  # retry at 10-04 11:00 succeeds
    [case] = await rows(session_factory, RecoveryCase)
    assert case.status == R.CANCELLED and case.recovery_outcome.value == "payment_succeeded"


async def test_scenario_outcome_is_stable_on_replay(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, seed=99)
    await sim.advance("demo-001", ist(2026, 12, 31))
    first = sorted(
        (i.subscription_id, i.billing_period, i.scenario_id)
        for i in await rows(session_factory, BillingInstallment)
    )
    await service.reset_simulation(session_factory, "demo-001")
    await sim.advance("demo-001", ist(2026, 12, 31))
    second = sorted(
        (i.subscription_id, i.billing_period, i.scenario_id)
        for i in await rows(session_factory, BillingInstallment)
    )
    assert first == second


# --- Idempotency ---------------------------------------------------------------------------


async def test_repeated_advance_and_idempotency_key(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(session_factory, settings, assignments=ALL_SUCCESS)
    first = await sim.advance("demo-001", ist(2026, 10, 10), idempotency_key="advance-0001")
    again = await sim.advance("demo-001", ist(2026, 10, 10), idempotency_key="advance-0001")
    assert again.replayed and again.advance.id == first.advance.id
    noop = await sim.advance("demo-001", ist(2026, 10, 10))  # same target, no key: no-op
    assert noop.advance.counters.get("events_processed", 0) == 0
    assert await count(session_factory, BillingInstallment) == 3
    with pytest.raises(ConflictError, match="different advance"):
        await sim.advance("demo-001", ist(2026, 10, 20), idempotency_key="advance-0001")


async def test_duplicate_installments_and_cases_are_rejected_by_the_database(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "MANDATE_INACTIVE"}
    )
    await sim.advance("demo-001", ist(2026, 10, 2, 12))
    [installment] = await rows(
        session_factory, BillingInstallment, BillingInstallment.customer_id == "CUS-2001"
    )
    async with session_factory() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                s.add(
                    BillingInstallment(
                        simulation_id="demo-001",
                        subscription_id="SUB-3001",
                        customer_id="CUS-2001",
                        billing_period="2026-10",
                        due_at=installment.due_at,
                        amount_minor_units=1,
                        currency=installment.currency,
                        payment_status=I.SCHEDULED,
                        scenario_id="SUCCESSFUL_PAYMENT",
                        idempotency_key="different-key",
                    )
                )
    async with session_factory() as s, s.begin():
        fresh = await s.get(BillingInstallment, installment.installment_id)
        assert fresh is not None
        assert (
            await recovery_service.open_case(s, fresh, failed_at=START, eligible_at=START) is None
        )
    assert await count(session_factory, RecoveryCase) == 1


async def test_reprocessing_a_processed_event_is_a_no_op(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "MANDATE_INACTIVE"}
    )
    await sim.advance("demo-001", ist(2026, 10, 2, 12))
    async with session_factory() as s, s.begin():  # simulate duplicate delivery
        await s.execute(
            SimulationEvent.__table__.update()
            .where(
                SimulationEvent.event_type.in_(
                    [EventType.INSTALLMENT_DUE, EventType.PAYMENT_FAILED]
                )
            )
            .values(status=EventStatus.PENDING)
        )
    await sim.advance("demo-001", ist(2026, 10, 2, 13))
    assert (
        await count(
            session_factory, BillingInstallment, BillingInstallment.customer_id == "CUS-2001"
        )
        == 1
    )
    assert await count(session_factory, PaymentAttempt) == 1
    assert await count(session_factory, RecoveryCase) == 1
    assert (
        await count(session_factory, SimulationEvent, SimulationEvent.status == EventStatus.SKIPPED)
        >= 2
    )


async def test_failure_mid_advance_rolls_back_and_resumes(
    sim: EventProcessor,
    session_factory: Factory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3002": "MANDATE_INACTIVE"}
    )
    original = processor_module.HANDLERS[EventType.PAYMENT_FAILED]
    calls = {"n": 0}

    async def flaky(ctx: processor_module.EventContext) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return await original(ctx)

    monkeypatch.setitem(processor_module.HANDLERS, EventType.PAYMENT_FAILED, flaky)
    with pytest.raises(AdvanceIncompleteError):
        await sim.advance("demo-001", ist(2026, 10, 10))
    async with session_factory() as s:
        state = await s.get(SimulationState, "demo-001")
        failed = await s.scalar(
            select(SimulationEvent).where(SimulationEvent.event_type == EventType.PAYMENT_FAILED)
        )
        assert state is not None and failed is not None
        # Clock stopped at the last fully processed event, before the failed one.
        assert state.current_virtual_time == ist(2026, 10, 4, 11)
        assert failed.status == EventStatus.PENDING and failed.attempt_count == 1
        assert "transient failure" in (failed.last_error or "")
    assert await count(session_factory, RecoveryCase) == 0  # its effects were rolled back

    result = await sim.advance("demo-001", ist(2026, 10, 10))  # resume
    assert result.advance.final_time == ist(2026, 10, 10)
    assert await count(session_factory, RecoveryCase) == 1


async def test_dead_lettered_event_blocks_the_clock_until_retried(
    sim: EventProcessor,
    session_factory: Factory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3002": "MANDATE_INACTIVE"}
    )

    async def broken(ctx: processor_module.EventContext) -> str:
        raise RuntimeError("permanent failure")

    monkeypatch.setitem(processor_module.HANDLERS, EventType.PAYMENT_FAILED, broken)
    for _ in range(settings.simulation_max_event_attempts):
        with pytest.raises(AdvanceIncompleteError):
            await sim.advance("demo-001", ist(2026, 10, 10))
    with pytest.raises(AdvanceIncompleteError, match="dead-lettered"):
        await sim.advance("demo-001", ist(2026, 10, 10))
    async with session_factory() as s:
        state = await s.get(SimulationState, "demo-001")
        assert state is not None and state.current_virtual_time < ist(2026, 10, 5)

    monkeypatch.undo()
    [failed] = await rows(
        session_factory, SimulationEvent, SimulationEvent.status == EventStatus.FAILED
    )
    await service.retry_event(session_factory, failed.event_id)
    result = await sim.advance("demo-001", ist(2026, 10, 10))
    assert result.advance.status.value == "completed"


# --- Recovery timing and "no calls from the clock" -----------------------------------------


async def test_recovery_eligibility_timing_and_no_calls(
    sim: EventProcessor, session_factory: Factory, settings: Settings
) -> None:
    await create(
        session_factory, settings, assignments=ALL_SUCCESS | {"SUB-3001": "MANDATE_INACTIVE"}
    )
    await sim.advance("demo-001", ist(2026, 10, 3, 10, 59))
    [case] = await rows(session_factory, RecoveryCase)
    assert case.status == R.OPEN  # not eligible before the configured delay
    await sim.advance("demo-001", ist(2026, 10, 3, 11))
    [case] = await rows(session_factory, RecoveryCase)
    assert case.status == R.AWAITING_APPROVAL and case.approval_requested_at == ist(2026, 10, 3, 11)
    await sim.advance("demo-001", ist(2027, 3, 1))
    assert await count(session_factory, CallSession) == 0  # the clock never places calls


async def test_do_not_contact_customer_is_opted_out_not_queued(
    session_factory: Factory, settings: Settings
) -> None:
    await seed_customers(session_factory, [Spec(9, 2, do_not_contact=True)])
    processor = EventProcessor(session_factory, settings)
    await create(session_factory, settings, assignments={"SUB-3009": "MANDATE_INACTIVE"})
    await processor.advance("demo-001", ist(2026, 10, 5))
    [case] = await rows(session_factory, RecoveryCase)
    assert case.status == R.OPTED_OUT and case.recovery_outcome.value == "do_not_contact"


async def test_month_end_and_leap_year_billing(
    session_factory: Factory, settings: Settings
) -> None:
    await seed_customers(session_factory, [Spec(5, 31, anchor_month=1, anchor_year=2028)])
    processor = EventProcessor(session_factory, settings)
    await create(
        session_factory,
        settings,
        "leap",
        assignments={"SUB-3005": "SUCCESSFUL_PAYMENT"},
        initial_time=ist(2028, 1, 1),
    )
    await processor.advance("leap", ist(2028, 4, 30, 23))
    dues = sorted(
        i.due_at.astimezone(ist(2028, 1, 1).tzinfo).date().isoformat()
        for i in await rows(session_factory, BillingInstallment)
    )
    assert dues == ["2028-01-31", "2028-02-29", "2028-03-31", "2028-04-30"]


async def test_advance_target_rules(session_factory: Factory, settings: Settings) -> None:
    await create(session_factory, settings)
    async with session_factory() as s:
        state = await s.get(SimulationState, "demo-001")
        assert state is not None
        assert service.advance_target(
            state, advance_days=10, target_time=None, settings=settings
        ) == ist(2026, 10, 11)
        from src.common.exceptions import ValidationFailedError

        with pytest.raises(ValidationFailedError):
            service.advance_target(
                state, advance_days=1, target_time=ist(2026, 10, 2), settings=settings
            )
        with pytest.raises(ValidationFailedError):
            service.advance_target(
                state, advance_days=None, target_time=datetime(2026, 10, 2), settings=settings
            )
