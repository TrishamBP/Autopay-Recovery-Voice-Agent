"""Customer API contracts. Raw phone, email and payment-link URL are never returned."""

from datetime import date, datetime

from pydantic import Field

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
from src.common.money import optional_paise_to_rupees, paise_to_rupees
from src.common.redaction import mask_phone
from src.common.schemas import ApiModel, Money
from src.customers.models import Customer


class PaymentSummary(ApiModel):
    payment_history_summary: str
    payments_on_time_6m: int
    payments_late_6m: int
    payments_failed_6m: int
    payments_missed_6m: int
    minimum_amount_paid_6m: Money | None
    total_amount_paid_6m: Money
    payment_reliability_score: int | None = Field(
        description="round(100 x fully paid cycles / cycles). "
        "Operational indicator, NOT a credit score."
    )


class CustomerListItem(ApiModel):
    customer_id: str
    full_name: str
    subscription_plan: str
    payment_amount: Money
    currency: Currency
    payment_status: PaymentStatus
    recovery_status: RecoveryStatus
    do_not_contact: bool
    preferred_language: SupportedLanguage

    @classmethod
    def from_model(cls, c: Customer) -> "CustomerListItem":
        return cls(
            customer_id=c.customer_id,
            full_name=c.full_name,
            subscription_plan=c.subscription_plan,
            payment_amount=paise_to_rupees(c.payment_amount_paise),
            currency=c.currency,
            payment_status=c.payment_status,
            recovery_status=c.recovery_status,
            do_not_contact=c.do_not_contact,
            preferred_language=c.preferred_language,
        )


class CustomerDetail(CustomerListItem):
    phone_number_masked: str | None
    customer_contact_consent: ContactConsent = Field(
        description="synthetic_test_record is NOT consent to contact anyone"
    )
    subscription_id: str
    billing_frequency: BillingFrequency
    payment_due_date: date
    payment_failure_date: date | None
    payment_failure_reason: PaymentFailureReason | None
    payment_attempt_count: int
    last_payment_attempt_at: datetime | None
    next_retry_eligible_at: datetime | None
    payment_link_status: PaymentLinkStatus
    preferred_contact_time: str
    callback_requested_at: datetime | None
    last_call_status: CallStatus
    last_call_outcome: CallOutcome
    updated_at: datetime
    notes_updated_at: datetime | None
    summary: PaymentSummary

    @classmethod
    def from_model(cls, c: Customer) -> "CustomerDetail":
        base = CustomerListItem.from_model(c).model_dump()
        return cls(
            **base,
            phone_number_masked=mask_phone(c.phone_number),
            customer_contact_consent=c.customer_contact_consent,
            subscription_id=c.subscription_id,
            billing_frequency=c.billing_frequency,
            payment_due_date=c.payment_due_date,
            payment_failure_date=c.payment_failure_date,
            payment_failure_reason=c.payment_failure_reason,
            payment_attempt_count=c.payment_attempt_count,
            last_payment_attempt_at=c.last_payment_attempt_at,
            next_retry_eligible_at=c.next_retry_eligible_at,
            payment_link_status=c.payment_link_status,
            preferred_contact_time=c.preferred_contact_time,
            callback_requested_at=c.callback_requested_at,
            last_call_status=c.last_call_status,
            last_call_outcome=c.last_call_outcome,
            updated_at=c.updated_at,
            notes_updated_at=c.notes_updated_at,
            summary=PaymentSummary(
                payment_history_summary=c.payment_history_summary,
                payments_on_time_6m=c.payments_on_time_6m,
                payments_late_6m=c.payments_late_6m,
                payments_failed_6m=c.payments_failed_6m,
                payments_missed_6m=c.payments_missed_6m,
                minimum_amount_paid_6m=optional_paise_to_rupees(c.minimum_amount_paid_6m_paise),
                total_amount_paid_6m=paise_to_rupees(c.total_amount_paid_6m_paise),
                payment_reliability_score=c.payment_reliability_score,
            ),
        )
