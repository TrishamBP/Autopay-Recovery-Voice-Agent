"""Payment-history reads and the customer-summary refresh."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.money import optional_paise_to_rupees, paise_to_rupees, rupees_to_paise
from src.customers.models import Customer
from src.payment_history.models import PaymentHistory
from src.payment_history.summary import CycleFacts, HistorySummary, compute_summary


async def list_history(session: AsyncSession, customer_id: str) -> list[PaymentHistory]:
    result = await session.scalars(
        select(PaymentHistory)
        .where(PaymentHistory.customer_id == customer_id)
        .order_by(PaymentHistory.billing_month.desc())
    )
    return list(result)


def to_cycle_facts(row: PaymentHistory) -> CycleFacts:
    return CycleFacts(
        billing_month=row.billing_month,
        status=row.status,
        amount_paid=paise_to_rupees(row.amount_paid_paise),
        recovered_after_failure=row.recovered_after_failure,
    )


async def compute_customer_summary(session: AsyncSession, customer_id: str) -> HistorySummary:
    return compute_summary([to_cycle_facts(r) for r in await list_history(session, customer_id)])


def apply_summary(customer: Customer, summary: HistorySummary) -> None:
    customer.payment_history_summary = summary.payment_history_summary
    customer.payments_on_time_6m = summary.payments_on_time_6m
    customer.payments_late_6m = summary.payments_late_6m
    customer.payments_failed_6m = summary.payments_failed_6m
    customer.payments_missed_6m = summary.payments_missed_6m
    customer.minimum_amount_paid_6m_paise = (
        None
        if summary.minimum_amount_paid_6m is None
        else rupees_to_paise(summary.minimum_amount_paid_6m)
    )
    customer.total_amount_paid_6m_paise = rupees_to_paise(summary.total_amount_paid_6m)
    customer.payment_reliability_score = summary.payment_reliability_score


def stored_summary(customer: Customer) -> HistorySummary:
    return HistorySummary(
        payment_history_summary=customer.payment_history_summary,
        payments_on_time_6m=customer.payments_on_time_6m,
        payments_late_6m=customer.payments_late_6m,
        payments_failed_6m=customer.payments_failed_6m,
        payments_missed_6m=customer.payments_missed_6m,
        minimum_amount_paid_6m=optional_paise_to_rupees(customer.minimum_amount_paid_6m_paise),
        total_amount_paid_6m=paise_to_rupees(customer.total_amount_paid_6m_paise),
        payment_reliability_score=customer.payment_reliability_score,
    )


async def refresh_customer_summary(session: AsyncSession, customer: Customer) -> HistorySummary:
    """Recompute the cached summary from canonical rows. Call inside the caller's transaction."""
    summary = await compute_customer_summary(session, customer.customer_id)
    apply_summary(customer, summary)
    return summary
