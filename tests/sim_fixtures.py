"""Deterministic customers + subscriptions for simulation tests (no workbook, no network)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.billing.models import Subscription
from src.common.enums import (
    BillingFrequency,
    CallOutcome,
    CallStatus,
    ContactConsent,
    Currency,
    PaymentLinkStatus,
    PaymentStatus,
    RecoveryStatus,
    SupportedLanguage,
)
from src.customers.models import Customer

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 10, 1, tzinfo=IST)


def ist(*args: int) -> datetime:
    return datetime(*args, tzinfo=IST)  # type: ignore[misc]


@dataclass(frozen=True)
class Spec:
    number: int
    due_day: int
    amount_paise: int = 79900
    frequency: BillingFrequency = BillingFrequency.MONTHLY
    anchor_month: int = 10
    anchor_year: int = 2026
    contact_time: str = "Any day 9 AM - 8 PM IST"
    do_not_contact: bool = False

    @property
    def customer_id(self) -> str:
        return f"CUS-{2000 + self.number}"

    @property
    def subscription_id(self) -> str:
        return f"SUB-{3000 + self.number}"


def three_customers() -> list[Spec]:
    return [Spec(1, 2), Spec(2, 4), Spec(3, 6)]


async def seed_customers(factory: async_sessionmaker[AsyncSession], specs: list[Spec]) -> None:
    async with factory() as session, session.begin():
        for s in specs:
            anchor = date(s.anchor_year, s.anchor_month, s.due_day)
            session.add(
                Customer(
                    customer_id=s.customer_id,
                    full_name=f"Test Customer {s.number}",
                    phone_number=f"+91900000{9000 + s.number}",
                    email=f"test{s.number}@example.com",
                    customer_contact_consent=ContactConsent.SYNTHETIC_TEST_RECORD,
                    preferred_language=SupportedLanguage.ENGLISH,
                    preferred_contact_time=s.contact_time,
                    do_not_contact=s.do_not_contact,
                    subscription_id=s.subscription_id,
                    subscription_plan="Test Plan",
                    payment_amount_paise=s.amount_paise,
                    currency=Currency.INR,
                    billing_frequency=s.frequency,
                    payment_due_date=anchor,
                    payment_status=PaymentStatus.FAILED,
                    payment_attempt_count=1,
                    payment_link_status=PaymentLinkStatus.NOT_CREATED,
                    recovery_status=RecoveryStatus.PENDING,
                    last_call_status=CallStatus.NOT_CALLED,
                    last_call_outcome=CallOutcome.NOT_ATTEMPTED,
                    updated_at=datetime(2026, 9, 1, tzinfo=UTC),
                )
            )
            await session.flush()
            session.add(
                Subscription(
                    subscription_id=s.subscription_id,
                    customer_id=s.customer_id,
                    billing_frequency=s.frequency,
                    billing_anchor_date=anchor,
                    billing_day_of_month=s.due_day,
                    amount_minor_units=s.amount_paise,
                    currency=Currency.INR,
                    is_active=True,
                    recovery_policy_id="standard",
                )
            )
