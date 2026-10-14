"""Simulation API contracts. Virtual times are rendered in the simulation's time zone."""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.simulation.events import EventStatus, EventType
from src.simulation.models import SimulationAdvance, SimulationEvent, SimulationState

SimulationId = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScenarioAssignmentIn(Strict):
    subscription_id: str = Field(pattern=r"^SUB-\d{4}$")
    billing_period: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2})?$")
    scenario_id: str


class SimulationCreate(Strict):
    simulation_id: str = SimulationId
    initial_time: datetime | None = None
    timezone: str | None = None
    seed: int | None = Field(default=None, ge=0)
    scenario_assignments: list[ScenarioAssignmentIn] = Field(default_factory=list, max_length=500)


class SimulationStateOut(BaseModel):
    simulation_id: str
    current_time: datetime
    initial_time: datetime
    timezone: str
    status: str
    seed: int
    configuration_version: int
    generation: int

    @classmethod
    def from_model(cls, sim: SimulationState) -> "SimulationStateOut":
        tz = ZoneInfo(sim.timezone)
        return cls(
            simulation_id=sim.simulation_id,
            current_time=sim.current_virtual_time.astimezone(tz),
            initial_time=sim.initial_virtual_time.astimezone(tz),
            timezone=sim.timezone,
            status=sim.status.value,
            seed=sim.seed,
            configuration_version=sim.configuration_version,
            generation=sim.generation,
        )


class AdvanceRequest(Strict):
    simulation_id: str = SimulationId
    advance_days: int | None = Field(default=None, gt=0)
    target_time: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _exactly_one(self) -> "AdvanceRequest":
        if (self.advance_days is None) == (self.target_time is None):
            raise ValueError("provide exactly one of advance_days or target_time")
        return self


class AdvanceResponse(BaseModel):
    simulation_id: str
    advance_id: str
    status: str
    previous_time: datetime
    current_time: datetime
    events_created: int
    events_processed: int
    events_skipped: int
    installments_created: int
    payment_attempts_created: int
    recovery_cases_created: int
    recovery_cases_queued: int
    calls_initiated: int = Field(0, description="Always 0: advancing time never places calls")
    replayed: bool = False

    @classmethod
    def from_run(cls, run: SimulationAdvance, tz: str, replayed: bool) -> "AdvanceResponse":
        zone, c = ZoneInfo(tz), run.counters
        return cls(
            simulation_id=run.simulation_id,
            advance_id=run.id,
            status=run.status.value,
            previous_time=run.previous_time.astimezone(zone),
            current_time=(run.final_time or run.previous_time).astimezone(zone),
            events_created=c.get("events_created", 0),
            events_processed=c.get("events_processed", 0),
            events_skipped=c.get("skipped", 0),
            installments_created=c.get("installments_created", 0),
            payment_attempts_created=c.get("payment_attempts_created", 0),
            recovery_cases_created=c.get("recovery_cases_created", 0),
            recovery_cases_queued=c.get("recovery_cases_queued", 0),
            replayed=replayed,
        )


class ResetRequest(Strict):
    simulation_id: str = SimulationId
    confirm: str = Field(description="Must equal 'RESET <simulation_id>'")
    initial_time: datetime | None = None


class EventOut(BaseModel):
    event_id: str
    sequence: int
    event_type: EventType
    aggregate_type: str
    aggregate_id: str
    scheduled_at_virtual: datetime
    status: EventStatus
    outcome: str | None
    attempt_count: int
    last_error: str | None
    payload: dict[str, object]
    processed_at_virtual: datetime | None
    processed_at_real: datetime | None

    @classmethod
    def from_model(cls, e: SimulationEvent, tz: ZoneInfo) -> "EventOut":
        return cls(
            event_id=e.event_id,
            sequence=e.sequence,
            event_type=e.event_type,
            aggregate_type=e.aggregate_type,
            aggregate_id=e.aggregate_id,
            scheduled_at_virtual=e.scheduled_at_virtual.astimezone(tz),
            status=e.status,
            outcome=e.outcome,
            attempt_count=e.attempt_count,
            last_error=e.last_error,
            payload=e.payload,
            processed_at_virtual=e.processed_at_virtual.astimezone(tz)
            if e.processed_at_virtual
            else None,
            processed_at_real=e.processed_at_real,
        )


class ScenarioOut(BaseModel):
    scenario_id: str
    description: str
    attempt_outcomes: list[str]
    failure_reason: str | None
    retry_permitted: bool
    retry_delay_hours: float
    max_attempts: int
    recovery_delay_hours: float
    reconciliation_delay_hours: float | None


class CallStatusIn(Strict):
    status: str = Field(description="Provider status, e.g. ringing, in-progress, completed, busy")
    sequence: int | None = Field(default=None, ge=0)
