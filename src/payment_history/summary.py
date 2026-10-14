"""Six-month payment summary, derived ONLY from `payment_history` rows.

Window: the customer's most recent `SUMMARY_WINDOW_CYCLES` billing cycles by `billing_month`
(one row per cycle, so retries are never counted as extra cycles).

Field definitions (n = number of cycles in the window, at most 6):
- payments_on_time_6m        count(status = paid_on_time)
- payments_late_6m           count(status = paid_late)
- payments_failed_6m         count(status = failed)    -- open, unrecovered cycle
- payments_missed_6m         count(status = missed)    -- closed cycle left unpaid
- (partially paid cycles are counted in the text summary; they are NOT fully paid)
- minimum_amount_paid_6m     min(amount_paid) over cycles with amount_paid > 0; None if none
- total_amount_paid_6m       sum(amount_paid) over the window (partials included)
- payment_reliability_score  round_half_up(100 * (on_time + late) / n); None when n = 0.
                             An operational indicator for conversation context, NOT a credit
                             score, and must not drive eligibility decisions.
- payment_history_summary    text rendering of the above (format matches the dataset generator)
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.common.enums import PaymentHistoryStatus as S
from src.common.money import TWO_PLACES

SUMMARY_WINDOW_CYCLES = 6


@dataclass(frozen=True)
class CycleFacts:
    billing_month: str  # YYYY-MM
    status: S
    amount_paid: Decimal
    recovered_after_failure: bool


@dataclass(frozen=True)
class HistorySummary:
    payment_history_summary: str
    payments_on_time_6m: int
    payments_late_6m: int
    payments_failed_6m: int
    payments_missed_6m: int
    minimum_amount_paid_6m: Decimal | None
    total_amount_paid_6m: Decimal
    payment_reliability_score: int | None


def reliability_score(fully_paid: int, cycles: int) -> int | None:
    """round(100 * fully_paid / cycles), half-up, in exact integer arithmetic."""
    if cycles == 0:
        return None
    return (200 * fully_paid + cycles) // (2 * cycles)


def _month_label(billing_month: str) -> str:
    return datetime.strptime(billing_month, "%Y-%m").strftime("%b %Y")  # noqa: DTZ007  label only


def compute_summary(cycles: Sequence[CycleFacts]) -> HistorySummary:
    window = sorted(cycles, key=lambda c: c.billing_month)[-SUMMARY_WINDOW_CYCLES:]
    counts = {status: sum(1 for c in window if c.status == status) for status in S}
    received = [c.amount_paid for c in window if c.amount_paid > 0]
    total = sum(received, Decimal("0")).quantize(TWO_PLACES)
    recovered = sum(1 for c in window if c.recovered_after_failure)
    fully_paid = counts[S.PAID_ON_TIME] + counts[S.PAID_LATE]

    if window:
        text = (
            f"{_month_label(window[0].billing_month)} to {_month_label(window[-1].billing_month)}: "
            f"{counts[S.PAID_ON_TIME]} on time, {counts[S.PAID_LATE]} late, "
            f"{counts[S.PARTIALLY_PAID]} partially paid, {counts[S.MISSED]} missed, "
            f"{counts[S.FAILED]} failed (open). {recovered} cycle{'s' if recovered != 1 else ''} "
            f"recovered after an autopay failure. Total received INR {total:,.2f}."
        )
    else:
        text = "No billing cycles recorded."

    return HistorySummary(
        payment_history_summary=text,
        payments_on_time_6m=counts[S.PAID_ON_TIME],
        payments_late_6m=counts[S.PAID_LATE],
        payments_failed_6m=counts[S.FAILED],
        payments_missed_6m=counts[S.MISSED],
        minimum_amount_paid_6m=min(received) if received else None,
        total_amount_paid_6m=total,
        payment_reliability_score=reliability_score(fully_paid, len(window)),
    )
