"""Current-charge reads and SIMULATED payment links (no real payment, no real charge)."""

import secrets
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import record_audit
from src.common.enums import ActorType, PaymentLinkPurpose, PaymentLinkStatus, PaymentStatus
from src.common.exceptions import BusinessRuleViolation
from src.common.money import paise_to_rupees
from src.common.time import Clock, utc_now
from src.config import Settings
from src.customers.models import Customer
from src.customers.service import assert_contactable, get_customer_for_update
from src.payments.models import PaymentLink

_LINKABLE_STATUSES = {PaymentStatus.FAILED, PaymentStatus.RETRY_SCHEDULED}


def payment_status_view(customer: Customer) -> dict[str, object]:
    """Facts the agent may state. Never includes the link URL or contact details."""
    return {
        "payment_status": customer.payment_status.value,
        "amount": f"{paise_to_rupees(customer.payment_amount_paise):.2f}",
        "currency": customer.currency.value,
        "subscription_plan": customer.subscription_plan,
        "payment_due_date": customer.payment_due_date.isoformat(),
        "payment_failure_date": (
            customer.payment_failure_date.isoformat() if customer.payment_failure_date else None
        ),
        "payment_failure_reason": (
            customer.payment_failure_reason.value if customer.payment_failure_reason else None
        ),
        "payment_attempt_count": customer.payment_attempt_count,
        "next_retry_eligible_at": (
            customer.next_retry_eligible_at.isoformat() if customer.next_retry_eligible_at else None
        ),
        "payment_link_status": customer.payment_link_status.value,
        "recovery_status": customer.recovery_status.value,
    }


async def create_simulated_payment_link(
    session: AsyncSession,
    *,
    customer_id: str,
    purpose: PaymentLinkPurpose,
    settings: Settings,
    actor_type: ActorType,
    call_session_id: str | None = None,
    clock: Clock = utc_now,
) -> PaymentLink:
    """Create a clearly simulated link. It is NOT delivered anywhere (no delivery channel exists
    yet), so status is `created`, never `sent`, and payment status is untouched."""
    customer = await get_customer_for_update(session, customer_id)
    assert_contactable(customer)
    if customer.payment_status not in _LINKABLE_STATUSES:
        raise BusinessRuleViolation(
            f"a payment link cannot be created while payment_status is {customer.payment_status}"
        )
    now = clock()
    link = PaymentLink(
        customer_id=customer.customer_id,
        call_session_id=call_session_id,
        purpose=purpose,
        token=secrets.token_urlsafe(24),
        status=PaymentLinkStatus.CREATED,
        expires_at=now + timedelta(hours=settings.payment_link_ttl_hours),
        created_at=now,
    )
    session.add(link)
    await session.flush()
    customer.payment_link_status = PaymentLinkStatus.CREATED
    customer.payment_link_url = f"{settings.simulated_payment_base_url}/{link.token}"
    customer.updated_at = now
    record_audit(
        session,
        event_type="payment_link.created",
        entity_type="payment_link",
        entity_id=link.id,
        actor_type=actor_type,
        call_session_id=call_session_id,
        details={"customer_id": customer.customer_id, "purpose": purpose.value, "simulated": True},
    )
    return link
