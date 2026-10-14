"""Customer reads and backend-enforced contact rules."""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import record_audit
from src.common.enums import ActorType, PaymentStatus, RecoveryStatus
from src.common.exceptions import BusinessRuleViolation, NotFoundError
from src.common.time import Clock, utc_now
from src.customers.models import Customer


async def get_customer(session: AsyncSession, customer_id: str) -> Customer:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    return customer


async def get_customer_for_update(session: AsyncSession, customer_id: str) -> Customer:
    """Load inside a write transaction. `with_for_update` is a no-op on SQLite (single writer)
    and a row lock on PostgreSQL."""
    customer = await session.scalar(
        select(Customer).where(Customer.customer_id == customer_id).with_for_update()
    )
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    return customer


async def list_customers(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[Sequence[Customer], int]:
    total = await session.scalar(select(func.count()).select_from(Customer)) or 0
    rows = await session.scalars(
        select(Customer).order_by(Customer.customer_id).limit(limit).offset(offset)
    )
    return rows.all(), total


def assert_contactable(customer: Customer) -> None:
    """Enforced in code for every contact action (callbacks, links, future dialling) — not a
    prompt instruction. `customer_contact_consent='synthetic_test_record'` is NOT consent; real
    dialling is additionally restricted to an allowlist of tester-controlled numbers."""
    if customer.do_not_contact or customer.recovery_status == RecoveryStatus.OPTED_OUT:
        raise BusinessRuleViolation("Customer has opted out of contact")
    if customer.payment_status in (PaymentStatus.PAID, PaymentStatus.CANCELLED):
        raise BusinessRuleViolation("No recovery contact is allowed for this charge")


async def mark_do_not_contact(
    session: AsyncSession,
    customer_id: str,
    *,
    actor_type: ActorType,
    call_session_id: str | None = None,
    clock: Clock = utc_now,
) -> bool:
    """One-way opt-out. Returns True if the flag changed. There is deliberately no API or tool
    that sets it back to False; reversing an opt-out is a manual, audited CRM action."""
    customer = await get_customer_for_update(session, customer_id)
    if customer.do_not_contact:
        return False
    customer.do_not_contact = True
    customer.recovery_status = RecoveryStatus.OPTED_OUT
    customer.updated_at = clock()
    record_audit(
        session,
        event_type="customer.do_not_contact_set",
        entity_type="customer",
        entity_id=customer.customer_id,
        actor_type=actor_type,
        call_session_id=call_session_id,
        details={"recovery_status": RecoveryStatus.OPTED_OUT.value},
    )
    return True
