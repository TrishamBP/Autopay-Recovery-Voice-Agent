"""Billing tables.

`subscription` is global (one per imported customer for this assignment; created by the importer
from the workbook's subscription columns). `billing_installment` and `payment_attempt` belong to a
simulation, so simulations never share billing state. Money is integer minor units (paise).
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.billing.state_machine import AttemptOutcome, InstallmentStatus, PaymentSource
from src.common.enums import BillingFrequency, Currency, PaymentFailureReason
from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        CheckConstraint("amount_minor_units > 0", name="amount_positive"),
        CheckConstraint("billing_day_of_month BETWEEN 1 AND 31", name="billing_day_range"),
    )

    subscription_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    billing_frequency: Mapped[BillingFrequency] = mapped_column(
        enum_column(BillingFrequency, "billing_frequency")
    )
    # Anchor date: the day (monthly) or month+day (annual) every due date is derived from.
    # Days past a month's end clamp to its last day (31st -> 30th/28th/29th).
    billing_anchor_date: Mapped[date] = mapped_column(Date)
    billing_day_of_month: Mapped[int] = mapped_column(Integer)
    amount_minor_units: Mapped[int] = mapped_column(Integer)
    currency: Mapped[Currency] = mapped_column(enum_column(Currency, "currency"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
    recovery_policy_id: Mapped[str] = mapped_column(String(32), server_default="standard")


class BillingInstallment(Base):
    __tablename__ = "billing_installment"
    __table_args__ = (
        UniqueConstraint("simulation_id", "subscription_id", "billing_period"),
        CheckConstraint("amount_minor_units > 0", name="amount_positive"),
        Index("ix_billing_installment_simulation_status", "simulation_id", "payment_status"),
    )

    installment_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="CASCADE")
    )
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscription.subscription_id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    billing_period: Mapped[str] = mapped_column(String(7))  # YYYY-MM (monthly) or YYYY (annual)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)  # virtual time
    amount_minor_units: Mapped[int] = mapped_column(Integer)
    currency: Mapped[Currency] = mapped_column(enum_column(Currency, "currency"))
    payment_status: Mapped[InstallmentStatus] = mapped_column(
        enum_column(InstallmentStatus, "payment_status")
    )
    scenario_id: Mapped[str] = mapped_column(String(64))
    recovery_eligible_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paid_at_virtual: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payment_source: Mapped[PaymentSource | None] = mapped_column(
        enum_column(PaymentSource, "payment_source")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempt"
    __table_args__ = (
        UniqueConstraint("installment_id", "attempt_number"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
    )

    payment_attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    installment_id: Mapped[str] = mapped_column(
        ForeignKey("billing_installment.installment_id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[AttemptOutcome] = mapped_column(enum_column(AttemptOutcome, "outcome"))
    failure_reason: Mapped[PaymentFailureReason | None] = mapped_column(
        enum_column(PaymentFailureReason, "failure_reason")
    )
    attempted_at_virtual: Mapped[datetime] = mapped_column(UTCDateTime)
    processed_at_real: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    reconciled_at_virtual: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scenario_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    attempt_metadata: Mapped[dict[str, object] | None] = mapped_column("metadata", JSON)
