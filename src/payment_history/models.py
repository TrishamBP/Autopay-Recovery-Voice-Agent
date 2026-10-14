"""`payment_history`: one row per customer per billing cycle.

`status` is the FINAL outcome of the cycle. Intermediate autopay attempts are summarised by
`failure_reason` (first failed attempt), `retry_count` (failed retries after the initial attempt)
and `recovered_after_failure`. A cycle is never counted twice, however many attempts it had.

Status semantics (enforced by CHECK constraints below and by the importer):
- paid_on_time   amount_paid == amount_due, payment_date <= due_date, days_late = 0
- paid_late      amount_paid == amount_due, payment_date >  due_date, days_late >= 1
- partially_paid 0 < amount_paid < amount_due; minimum_amount_paid = true; NOT fully paid
- failed         amount_paid = 0, no payment_date/days_late; only the open (latest) cycle
- missed         amount_paid = 0, no payment_date/days_late; a closed cycle left unpaid
"""

from datetime import date

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.common.enums import PaymentFailureReason, PaymentHistoryStatus, PaymentMethodType
from src.common.types import enum_column
from src.database import Base


class PaymentHistory(Base):
    __tablename__ = "payment_history"
    __table_args__ = (
        UniqueConstraint("customer_id", "billing_month"),
        CheckConstraint("history_id LIKE 'HIST-%'", name="history_id_format"),
        CheckConstraint("billing_month LIKE '____-__'", name="billing_month_format"),
        CheckConstraint("amount_due_paise > 0", name="amount_due_positive"),
        CheckConstraint(
            "amount_paid_paise >= 0 AND amount_paid_paise <= amount_due_paise",
            name="amount_paid_range",
        ),
        CheckConstraint("retry_count BETWEEN 0 AND 2", name="retry_count_range"),
        CheckConstraint("days_late IS NULL OR days_late >= 0", name="days_late_nonnegative"),
        CheckConstraint(
            "status NOT IN ('paid_on_time', 'paid_late') OR "
            "(amount_paid_paise = amount_due_paise AND payment_date IS NOT NULL)",
            name="fully_paid_consistent",
        ),
        CheckConstraint(
            "status != 'paid_on_time' OR days_late = 0", name="on_time_has_zero_days_late"
        ),
        CheckConstraint("status != 'paid_late' OR days_late >= 1", name="late_has_days_late"),
        CheckConstraint(
            "status != 'partially_paid' OR "
            "(amount_paid_paise > 0 AND amount_paid_paise < amount_due_paise "
            "AND payment_date IS NOT NULL)",
            name="partial_payment_consistent",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'missed') OR "
            "(amount_paid_paise = 0 AND payment_date IS NULL AND days_late IS NULL)",
            name="unpaid_has_no_payment",
        ),
        CheckConstraint(
            "minimum_amount_paid = (status = 'partially_paid')",
            name="minimum_amount_paid_flag",
        ),
        CheckConstraint(
            "NOT recovered_after_failure OR "
            "(status IN ('paid_on_time', 'paid_late') AND failure_reason IS NOT NULL)",
            name="recovered_consistent",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'missed', 'partially_paid') OR failure_reason IS NOT NULL",
            name="unpaid_has_failure_reason",
        ),
        CheckConstraint(
            "failure_reason IS NOT NULL OR retry_count = 0", name="retries_need_failure"
        ),
    )

    history_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    billing_month: Mapped[str] = mapped_column(String(7), index=True)
    due_date: Mapped[date] = mapped_column(Date)
    amount_due_paise: Mapped[int] = mapped_column(Integer)
    amount_paid_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[PaymentHistoryStatus] = mapped_column(
        enum_column(PaymentHistoryStatus, "status"), index=True
    )
    payment_date: Mapped[date | None] = mapped_column(Date)
    days_late: Mapped[int | None] = mapped_column(Integer)
    minimum_amount_paid: Mapped[bool] = mapped_column(Boolean)
    payment_method: Mapped[PaymentMethodType] = mapped_column(
        enum_column(PaymentMethodType, "payment_method")
    )
    failure_reason: Mapped[PaymentFailureReason | None] = mapped_column(
        enum_column(PaymentFailureReason, "failure_reason")
    )
    retry_count: Mapped[int] = mapped_column(Integer)
    recovered_after_failure: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
