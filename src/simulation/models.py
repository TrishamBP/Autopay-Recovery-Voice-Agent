"""Simulation clock, event ledger, advance runs and scenario assignments.

The virtual clock is persisted (`simulation_state.current_virtual_time`); process memory never
holds authoritative simulation time. Each simulation has its own clock and event stream.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base
from src.simulation.events import AdvanceStatus, EventStatus, EventType, SimulationStatus

ALL_PERIODS = "*"  # scenario_assignment.billing_period wildcard (NULLs are not unique in SQL)


class SimulationState(Base):
    __tablename__ = "simulation_state"
    __table_args__ = (
        CheckConstraint(
            "current_virtual_time >= initial_virtual_time", name="clock_not_before_start"
        ),
    )

    simulation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_virtual_time: Mapped[datetime] = mapped_column(UTCDateTime)
    initial_virtual_time: Mapped[datetime] = mapped_column(UTCDateTime)
    timezone: Mapped[str] = mapped_column(String(64))
    status: Mapped[SimulationStatus] = mapped_column(enum_column(SimulationStatus, "status"))
    seed: Mapped[int] = mapped_column(Integer)
    configuration_version: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=1)  # bumped on every reset
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class SimulationEvent(Base):
    __tablename__ = "simulation_event"
    __table_args__ = (
        UniqueConstraint("simulation_id", "idempotency_key"),
        UniqueConstraint("simulation_id", "sequence"),
        Index(
            "ix_simulation_event_pending",
            "simulation_id",
            "status",
            "scheduled_at_virtual",
            "sequence",
        ),
        Index("ix_simulation_event_aggregate", "aggregate_type", "aggregate_id"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)  # stable tie-breaker within a virtual instant
    event_type: Mapped[EventType] = mapped_column(enum_column(EventType, "event_type"))
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    scheduled_at_virtual: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[EventStatus] = mapped_column(enum_column(EventStatus, "status"))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    outcome: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    processed_at_virtual: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_at_real: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SimulationAdvance(Base):
    """One clock-advance request: makes advances resumable and safely repeatable."""

    __tablename__ = "simulation_advance"
    __table_args__ = (UniqueConstraint("simulation_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer)
    previous_time: Mapped[datetime] = mapped_column(UTCDateTime)
    target_time: Mapped[datetime] = mapped_column(UTCDateTime)
    final_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[AdvanceStatus] = mapped_column(enum_column(AdvanceStatus, "status"))
    counters: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ScenarioAssignment(Base):
    """Explicit scenario for a subscription in one billing period, or every period ('*')."""

    __tablename__ = "scenario_assignment"
    __table_args__ = (UniqueConstraint("simulation_id", "subscription_id", "billing_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="CASCADE")
    )
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscription.subscription_id", ondelete="RESTRICT")
    )
    billing_period: Mapped[str] = mapped_column(String(7), default=ALL_PERIODS)
    scenario_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
