"""Payment-history API contract."""

from datetime import date

from src.common.enums import PaymentFailureReason, PaymentHistoryStatus, PaymentMethodType
from src.common.money import paise_to_rupees
from src.common.schemas import ApiModel, Money
from src.payment_history.models import PaymentHistory


class PaymentHistoryItem(ApiModel):
    history_id: str
    billing_month: str
    due_date: date
    amount_due: Money
    amount_paid: Money
    status: PaymentHistoryStatus
    payment_date: date | None
    days_late: int | None
    minimum_amount_paid: bool
    payment_method: PaymentMethodType
    failure_reason: PaymentFailureReason | None
    retry_count: int
    recovered_after_failure: bool
    notes: str | None

    @classmethod
    def from_model(cls, h: PaymentHistory) -> "PaymentHistoryItem":
        return cls(
            history_id=h.history_id,
            billing_month=h.billing_month,
            due_date=h.due_date,
            amount_due=paise_to_rupees(h.amount_due_paise),
            amount_paid=paise_to_rupees(h.amount_paid_paise),
            status=h.status,
            payment_date=h.payment_date,
            days_late=h.days_late,
            minimum_amount_paid=h.minimum_amount_paid,
            payment_method=h.payment_method,
            failure_reason=h.failure_reason,
            retry_count=h.retry_count,
            recovered_after_failure=h.recovered_after_failure,
            notes=h.notes,
        )
