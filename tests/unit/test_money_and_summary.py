from decimal import Decimal

import pytest

from src.common.enums import PaymentHistoryStatus as S
from src.common.money import MoneyError, paise_to_rupees, rupees_to_paise, to_decimal
from src.payment_history.summary import CycleFacts, compute_summary, reliability_score


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (799, "799.00"),
        (Decimal("1499.5"), "1499.50"),
        (499.9, "499.90"),
        ("799", "799.00"),
        ("1,499.00", "1499.00"),
        ("₹799.00", "799.00"),
        ("INR 799", "799.00"),
        ("Rs. 10.5", "10.50"),
    ],
)
def test_to_decimal_parses_exactly(raw: object, expected: str) -> None:
    assert to_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [True, None, "abc", "1.234", 0.001, float("nan"), "₹", [], "$5"])
def test_to_decimal_rejects_invalid(raw: object) -> None:
    with pytest.raises(MoneyError):
        to_decimal(raw)


def test_paise_round_trip_is_exact() -> None:
    # 0.1 + 0.2 style float drift must not appear anywhere.
    amounts = [to_decimal("0.10"), to_decimal("0.20")]
    assert sum(rupees_to_paise(a) for a in amounts) == 30
    assert rupees_to_paise(Decimal("799.00")) == 79900
    assert paise_to_rupees(79900) == Decimal("799.00")
    assert paise_to_rupees(1) == Decimal("0.01")


def cycle(month: str, status: S, paid: str, recovered: bool = False) -> CycleFacts:
    return CycleFacts(f"2026-{month}", status, Decimal(paid), recovered)


def test_empty_history() -> None:
    s = compute_summary([])
    assert (s.payments_on_time_6m, s.payments_late_6m, s.payments_failed_6m) == (0, 0, 0)
    assert s.minimum_amount_paid_6m is None
    assert s.total_amount_paid_6m == Decimal("0.00")
    assert s.payment_reliability_score is None
    assert s.payment_history_summary == "No billing cycles recorded."


def test_mixed_history_with_partial_recovered_and_missed() -> None:
    s = compute_summary(
        [
            cycle("05", S.PAID_ON_TIME, "499"),
            cycle("06", S.PAID_LATE, "499", recovered=True),
            cycle("07", S.PARTIALLY_PAID, "210"),
            cycle("08", S.MISSED, "0"),
            cycle("09", S.PAID_ON_TIME, "499"),
            cycle("10", S.FAILED, "0"),
        ]
    )
    assert s.payments_on_time_6m == 2
    assert s.payments_late_6m == 1
    assert s.payments_missed_6m == 1
    assert s.payments_failed_6m == 1
    assert s.minimum_amount_paid_6m == Decimal("210")  # partial payments included
    assert s.total_amount_paid_6m == Decimal("1707.00")
    assert s.payment_reliability_score == 50  # partial is NOT fully paid: 3/6
    assert "1 cycle recovered" in s.payment_history_summary


def test_window_is_six_most_recent_cycles_and_retries_not_double_counted() -> None:
    rows = [cycle(f"{m:02d}", S.PAID_ON_TIME, "100") for m in range(1, 9)]  # 8 cycles
    s = compute_summary(rows)
    assert s.payments_on_time_6m == 6
    assert s.total_amount_paid_6m == Decimal("600.00")
    assert s.payment_history_summary.startswith("Mar 2026 to Aug 2026")


def test_input_order_does_not_matter() -> None:
    rows = [cycle("10", S.FAILED, "0"), cycle("09", S.PAID_LATE, "5", recovered=True)]
    assert compute_summary(rows) == compute_summary(list(reversed(rows)))


@pytest.mark.parametrize(
    ("fully_paid", "cycles", "expected"),
    [
        (5, 6, 83),
        (3, 6, 50),
        (1, 6, 17),
        (2, 6, 33),
        (4, 6, 67),
        (0, 6, 0),
        (6, 6, 100),
        (1, 8, 13),
    ],
)
def test_reliability_score_rounds_half_up(fully_paid: int, cycles: int, expected: int) -> None:
    assert reliability_score(fully_paid, cycles) == expected
