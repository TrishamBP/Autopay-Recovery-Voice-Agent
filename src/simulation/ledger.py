"""Event-ledger primitives. Callers own the transaction."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.time import utc_now
from src.simulation.events import EventStatus, EventType, event_key
from src.simulation.models import SimulationEvent


class SchedulingError(Exception):
    """A handler tried to schedule an event in the virtual past (would break ordering)."""


async def _next_sequence(session: AsyncSession, simulation_id: str) -> int:
    current = await session.scalar(
        select(func.max(SimulationEvent.sequence)).where(
            SimulationEvent.simulation_id == simulation_id
        )
    )
    return (current or 0) + 1


async def schedule_event(
    session: AsyncSession,
    *,
    simulation_id: str,
    event_type: EventType,
    aggregate_type: str,
    aggregate_id: str,
    discriminator: str,
    scheduled_at: datetime,
    payload: dict[str, object] | None = None,
    not_before: datetime | None = None,
) -> SimulationEvent | None:
    """Insert a PENDING event unless its idempotency key already exists (then None).

    `not_before` (the triggering event's time) rejects scheduling into the virtual past, which
    is how dependency cycles and ordering bugs surface instead of looping forever.
    """
    if not_before is not None and scheduled_at < not_before:
        raise SchedulingError(f"{event_type} scheduled before its cause ({scheduled_at})")
    key = event_key(simulation_id, event_type, aggregate_id, discriminator)
    exists = await session.scalar(
        select(SimulationEvent.event_id).where(
            SimulationEvent.simulation_id == simulation_id, SimulationEvent.idempotency_key == key
        )
    )
    if exists is not None:
        return None
    event = SimulationEvent(
        simulation_id=simulation_id,
        sequence=await _next_sequence(session, simulation_id),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        scheduled_at_virtual=scheduled_at,
        status=EventStatus.PENDING,
        idempotency_key=key,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def record_fact(
    session: AsyncSession,
    *,
    simulation_id: str,
    event_type: EventType,
    aggregate_type: str,
    aggregate_id: str,
    discriminator: str,
    virtual_time: datetime,
    payload: dict[str, object] | None = None,
    outcome: str = "recorded",
) -> SimulationEvent | None:
    """Ledger entry for something that already happened (approval, cancellation, call result).
    Written as PROCESSED so the processor never executes it."""
    event = await schedule_event(
        session,
        simulation_id=simulation_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        discriminator=discriminator,
        scheduled_at=virtual_time,
        payload=payload,
    )
    if event is not None:
        event.status = EventStatus.PROCESSED
        event.outcome = outcome
        event.processed_at_virtual = virtual_time
        event.processed_at_real = utc_now()
    return event
