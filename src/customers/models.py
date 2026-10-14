"""`customer`: one row per workbook `Customers` record.

Columns fall into four groups:
- identity/contact: owned by CRM; `phone_number` is E.164 (validated in application code).
- current failed charge: owned by billing/gateway events; `payment_status='paid'` only via a
  trusted confirmation event, never by the LLM.
- call/recovery cache: latest call state (canonical history lives in `call_session`).
- derived caches: six-month summary (from `payment_history`) and notes digest (from
  `customer_note`). Rewritten only by `payment_history.service.refresh_customer_summary` and
  `customer_notes.service.refresh_notes_digest`; never set directly.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from src.common.enums import (
    BillingFrequency,
    CallOutcome,
    CallStatus,
    ContactConsent,
    Currency,
    PaymentFailureReason,
    PaymentLinkStatus,
    PaymentStatus,
    RecoveryStatus,
    SupportedLanguage,
)
from src.common.types import UTCDateTime, enum_column
from src.database import Base


class Customer(Base):
    __tablename__ = "customer"
    __table_args__ = (
        CheckConstraint("customer_id LIKE 'CUS-%'", name="customer_id_format"),
        CheckConstraint("payment_amount_paise > 0", name="payment_amount_positive"),
        CheckConstraint("payment_attempt_count BETWEEN 1 AND 3", name="attempt_count_range"),
        CheckConstraint(
            "payments_on_time_6m >= 0 AND payments_late_6m >= 0 AND payments_failed_6m >= 0 "
            "AND payments_missed_6m >= 0",
            name="summary_counts_nonnegative",
        ),
        CheckConstraint(
            "minimum_amount_paid_6m_paise IS NULL OR minimum_amount_paid_6m_paise > 0",
            name="minimum_paid_positive",
        ),
        CheckConstraint("total_amount_paid_6m_paise >= 0", name="total_paid_nonnegative"),
        CheckConstraint(
            "payment_reliability_score IS NULL OR payment_reliability_score BETWEEN 0 AND 100",
            name="reliability_score_range",
        ),
    )

    # Identity / contact
    customer_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    phone_number: Mapped[str] = mapped_column(String(16), unique=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    customer_contact_consent: Mapped[ContactConsent] = mapped_column(
        enum_column(ContactConsent, "contact_consent")
    )
    preferred_language: Mapped[SupportedLanguage] = mapped_column(
        enum_column(SupportedLanguage, "preferred_language")
    )
    preferred_contact_time: Mapped[str] = mapped_column(String(100))
    do_not_contact: Mapped[bool] = mapped_column(Boolean, server_default=false(), index=True)

    # Current failed charge
    subscription_id: Mapped[str] = mapped_column(String(16), unique=True)
    subscription_plan: Mapped[str] = mapped_column(String(100), index=True)
    payment_amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[Currency] = mapped_column(enum_column(Currency, "currency"))
    billing_frequency: Mapped[BillingFrequency] = mapped_column(
        enum_column(BillingFrequency, "billing_frequency")
    )
    payment_due_date: Mapped[date] = mapped_column(Date)
    payment_failure_date: Mapped[date | None] = mapped_column(Date)
    payment_failure_reason: Mapped[PaymentFailureReason | None] = mapped_column(
        enum_column(PaymentFailureReason, "payment_failure_reason"), index=True
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        enum_column(PaymentStatus, "payment_status"), index=True
    )
    payment_attempt_count: Mapped[int] = mapped_column(Integer)
    last_payment_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_retry_eligible_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payment_link_status: Mapped[PaymentLinkStatus] = mapped_column(
        enum_column(PaymentLinkStatus, "payment_link_status")
    )
    payment_link_url: Mapped[str | None] = mapped_column(String(500))

    # Recovery / call cache
    recovery_status: Mapped[RecoveryStatus] = mapped_column(
        enum_column(RecoveryStatus, "recovery_status"), index=True
    )
    callback_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_call_id: Mapped[str | None] = mapped_column(String(64))
    last_call_status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus, "last_call_status")
    )
    last_call_outcome: Mapped[CallOutcome] = mapped_column(
        enum_column(CallOutcome, "last_call_outcome")
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    # Derived six-month summary (cache of payment_history)
    payment_history_summary: Mapped[str] = mapped_column(Text, server_default="")
    payments_on_time_6m: Mapped[int] = mapped_column(Integer, server_default="0")
    payments_late_6m: Mapped[int] = mapped_column(Integer, server_default="0")
    payments_failed_6m: Mapped[int] = mapped_column(Integer, server_default="0")
    payments_missed_6m: Mapped[int] = mapped_column(Integer, server_default="0")
    minimum_amount_paid_6m_paise: Mapped[int | None] = mapped_column(Integer)
    total_amount_paid_6m_paise: Mapped[int] = mapped_column(Integer, server_default="0")
    payment_reliability_score: Mapped[int | None] = mapped_column(Integer)

    # Derived notes digest (cache of customer_note)
    customer_notes: Mapped[str] = mapped_column(Text, server_default="")
    notes_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
