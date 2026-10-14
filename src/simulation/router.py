"""Simulation endpoints. Advancing time NEVER places calls.

Development-only endpoints (reset, scenario assignment, event retry, mock call status) answer
404 unless SIMULATION_DEV_ENDPOINTS_ENABLED=true and APP_ENV is development/test.
"""

from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, status

from src.common.dependencies import OperatorDep, SessionDep, SettingsDep
from src.common.exceptions import ConflictError, NotFoundError, ValidationFailedError
from src.common.schemas import ErrorResponse, Page
from src.simulation import service
from src.simulation.events import EventStatus, EventType
from src.simulation.processor import EventProcessor
from src.simulation.scenarios import CATALOG
from src.simulation.schemas import (
    AdvanceRequest,
    AdvanceResponse,
    CallStatusIn,
    EventOut,
    ResetRequest,
    ScenarioAssignmentIn,
    ScenarioOut,
    SimulationCreate,
    SimulationStateOut,
)
from src.telephony.status import apply_call_status

router = APIRouter(
    tags=["simulation"],
    dependencies=[OperatorDep],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
SimId = Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


def require_dev(settings: SettingsDep) -> None:
    if not settings.dev_endpoints_allowed:
        raise NotFoundError("Not Found")


DevOnly = [Depends(require_dev)]


def _processor(request: Request) -> EventProcessor:
    return request.app.state.processor


@router.post("/simulation", response_model=SimulationStateOut, status_code=status.HTTP_201_CREATED)
async def create_simulation(
    body: SimulationCreate, request: Request, settings: SettingsDep
) -> SimulationStateOut:
    sim, _ = await service.create_simulation(
        request.app.state.session_factory,
        settings,
        simulation_id=body.simulation_id,
        initial_time=body.initial_time,
        timezone=body.timezone,
        seed=body.seed,
        assignments=[
            (a.subscription_id, a.billing_period or "*", a.scenario_id)
            for a in body.scenario_assignments
        ],
    )
    return SimulationStateOut.from_model(sim)


@router.get("/simulation/state", response_model=SimulationStateOut)
async def get_state(simulation_id: SimId, session: SessionDep) -> SimulationStateOut:
    return SimulationStateOut.from_model(await service.get_simulation(session, simulation_id))


@router.post("/simulation/advance", response_model=AdvanceResponse)
async def advance(
    body: AdvanceRequest, request: Request, session: SessionDep, settings: SettingsDep
) -> AdvanceResponse:
    sim = await service.get_simulation(session, body.simulation_id)
    previous = (
        await service.find_advance(session, body.simulation_id, body.idempotency_key)
        if body.idempotency_key
        else None
    )
    if previous is not None and previous.generation == sim.generation:
        # A retried request: reuse the original target. `advance_days` is relative, so
        # recomputing it from the (already advanced) clock would wrongly look like a new request.
        if body.target_time is not None and body.target_time != previous.target_time:
            raise ConflictError("Idempotency key reused for a different advance")
        target = previous.target_time
    else:
        target = service.advance_target(
            sim, advance_days=body.advance_days, target_time=body.target_time, settings=settings
        )
    tz = sim.timezone
    await session.rollback()  # release the read before the processor's write transactions
    result = await _processor(request).advance(
        body.simulation_id, target, idempotency_key=body.idempotency_key
    )
    return AdvanceResponse.from_run(result.advance, tz, result.replayed)


@router.post("/simulation/reset", response_model=SimulationStateOut, dependencies=DevOnly)
async def reset(body: ResetRequest, request: Request) -> SimulationStateOut:
    if body.confirm != f"RESET {body.simulation_id}":
        raise ValidationFailedError(f"confirm must be exactly 'RESET {body.simulation_id}'")
    sim = await service.reset_simulation(
        request.app.state.session_factory, body.simulation_id, initial_time=body.initial_time
    )
    return SimulationStateOut.from_model(sim)


@router.get("/simulation/events", response_model=Page[EventOut])
async def list_events(
    simulation_id: SimId,
    session: SessionDep,
    status_filter: Annotated[EventStatus | None, Query(alias="status")] = None,
    event_type: EventType | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[EventOut]:
    sim = await service.get_simulation(session, simulation_id)
    rows, total = await service.list_events(
        session,
        simulation_id=simulation_id,
        status=status_filter,
        event_type=event_type,
        since=None,
        limit=limit,
        offset=offset,
    )
    tz = ZoneInfo(sim.timezone)
    return Page(
        items=[EventOut.from_model(e, tz) for e in rows], total=total, limit=limit, offset=offset
    )


@router.post("/simulation/events/{event_id}/retry", response_model=EventOut, dependencies=DevOnly)
async def retry_event(event_id: str, request: Request, session: SessionDep) -> EventOut:
    event = await service.retry_event(request.app.state.session_factory, event_id)
    sim = await service.get_simulation(session, event.simulation_id)
    return EventOut.from_model(event, ZoneInfo(sim.timezone))


@router.get("/simulation/scenarios", response_model=list[ScenarioOut])
async def list_scenarios() -> list[ScenarioOut]:
    return [
        ScenarioOut(
            scenario_id=s.scenario_id,
            description=s.description,
            attempt_outcomes=[o.value for o in s.attempt_outcomes],
            failure_reason=s.failure_reason.value if s.failure_reason else None,
            retry_permitted=s.retry_permitted,
            retry_delay_hours=s.retry_delay.total_seconds() / 3600,
            max_attempts=s.max_attempts,
            recovery_delay_hours=s.recovery_delay.total_seconds() / 3600,
            reconciliation_delay_hours=s.reconciliation_delay.total_seconds() / 3600
            if s.reconciliation_delay
            else None,
        )
        for s in CATALOG.values()
    ]


@router.post(
    "/simulation/scenario-assignments", status_code=status.HTTP_201_CREATED, dependencies=DevOnly
)
async def assign_scenario(
    simulation_id: SimId, body: ScenarioAssignmentIn, request: Request
) -> dict[str, str]:
    assignment = await service.assign_scenario(
        request.app.state.session_factory,
        simulation_id=simulation_id,
        subscription_id=body.subscription_id,
        billing_period=body.billing_period,
        scenario_id=body.scenario_id,
    )
    return {
        "assignment_id": assignment.id,
        "subscription_id": assignment.subscription_id,
        "billing_period": assignment.billing_period,
        "scenario_id": assignment.scenario_id,
    }


@router.post("/simulation/calls/{call_session_id}/status", dependencies=DevOnly)
async def mock_call_status(
    call_session_id: str, body: CallStatusIn, request: Request, settings: SettingsDep
) -> dict[str, str]:
    """Simulate a provider status callback for MOCK calls (Twilio uses the signed webhook)."""
    if settings.telephony_provider != "mock":
        raise NotFoundError("Not Found")
    result = await apply_call_status(
        request.app.state.session_factory,
        call_session_id=call_session_id,
        provider_status=body.status,
        provider_call_id=None,
        sequence=body.sequence,
    )
    return {"result": result}


@router.get("/summary")
async def simulation_summary(simulation_id: SimId, session: SessionDep) -> dict[str, object]:
    return await service.summary(session, simulation_id)
