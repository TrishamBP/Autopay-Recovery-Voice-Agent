"""Callback scheduling. Rules are enforced here, in code, regardless of what the LLM says."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import record_audit
from src.calls.models import CallbackRequest
from src.common.enums import ActorType, CallbackStatus, RecoveryStatus
from src.common.exceptions import BusinessRuleViolation
from src.common.time import Clock, utc_now
from src.config import Settings
from src.customers.service import assert_contactable, get_customer_for_update

# Recovery states a booked callback may move into `callback_scheduled`.
_CALLBACK_UPDATES_STATUS = {
    RecoveryStatus.PENDING,
    RecoveryStatus.IN_PROGRESS,
    RecoveryStatus.CALLBACK_SCHEDULED,
    RecoveryStatus.LINK_SENT,
}


def validate_callback_time(requested_at: datetime, settings: Settings, now: datetime) -> None:
    """Future (with a minimum lead), within the booking horizon, and inside permitted calling
    hours in the customer's local time zone."""
    if requested_at.tzinfo is None:
        raise BusinessRuleViolation("callback time must be timezone-aware")
    if requested_at < now + timedelta(minutes=settings.callback_min_lead_minutes):
        raise BusinessRuleViolation(
            f"callback time must be at least {settings.callback_min_lead_minutes} minutes ahead"
        )
    if requested_at > now + timedelta(days=settings.callback_max_horizon_days):
        raise BusinessRuleViolation(
            f"callback time must be within {settings.callback_max_horizon_days} days"
        )
    local = requested_at.astimezone(ZoneInfo(settings.customer_default_timezone))
    minutes = local.hour * 60 + local.minute
    if not settings.calling_hours_start * 60 <= minutes < settings.calling_hours_end * 60:
        raise BusinessRuleViolation(
            f"callback time must be between {settings.calling_hours_start:02d}:00 and "
            f"{settings.calling_hours_end:02d}:00 {settings.customer_default_timezone}"
        )


async def schedule_callback(
    session: AsyncSession,
    *,
    customer_id: str,
    requested_at: datetime,
    timezone: str,
    settings: Settings,
    actor_type: ActorType,
    call_session_id: str | None = None,
    clock: Clock = utc_now,
) -> CallbackRequest:
    """Book a callback. Does NOT change payment status: a callback is not a recovery."""
    customer = await get_customer_for_update(session, customer_id)
    assert_contactable(customer)
    now = clock()
    validate_callback_time(requested_at, settings, now)
    callback = CallbackRequest(
        customer_id=customer.customer_id,
        call_session_id=call_session_id,
        requested_at=requested_at,
        timezone=timezone,
        status=CallbackStatus.REQUESTED,
        created_at=now,
        updated_at=now,
    )
    session.add(callback)
    await session.flush()
    customer.callback_requested_at = requested_at
    if customer.recovery_status in _CALLBACK_UPDATES_STATUS:
        customer.recovery_status = RecoveryStatus.CALLBACK_SCHEDULED
    customer.updated_at = now
    record_audit(
        session,
        event_type="callback_request.created",
        entity_type="callback_request",
        entity_id=callback.id,
        actor_type=actor_type,
        call_session_id=call_session_id,
        details={
            "customer_id": customer.customer_id,
            "requested_at": requested_at.isoformat(),
            "timezone": timezone,
        },
    )
    return callback
