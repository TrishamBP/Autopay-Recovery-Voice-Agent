from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.billing.state_machine import InstallmentStatus as I
from src.billing.state_machine import InvalidTransitionError
from src.billing.state_machine import check_transition as installment_transition
from src.common.enums import BillingFrequency
from src.config import Settings
from src.recovery.policy import (
    consent_denial,
    contact_window_denial,
    destination_denial,
    parse_contact_window,
)
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.recovery.state_machine import check_transition as case_transition
from src.simulation.calendar import due_dates, local_instant
from src.simulation.scenarios import CATALOG, seeded_scenario_id

IST = ZoneInfo("Asia/Kolkata")
SETTINGS = Settings(
    _env_file=None,
    test_destination_allowlist="+15005550006,+919999999999",
    allow_synthetic_consent_for_test_destinations=True,
)


def months(anchor: date, start: date, end: date) -> list[tuple[str, date]]:
    return list(due_dates(BillingFrequency.MONTHLY, anchor, start, end))


def test_month_end_clamping_and_leap_years() -> None:
    dues = months(date(2026, 1, 31), date(2027, 11, 1), date(2028, 4, 30))
    assert dues == [
        ("2027-11", date(2027, 11, 30)),
        ("2027-12", date(2027, 12, 31)),
        ("2028-01", date(2028, 1, 31)),
        ("2028-02", date(2028, 2, 29)),  # leap year
        ("2028-03", date(2028, 3, 31)),
        ("2028-04", date(2028, 4, 30)),
    ]
    assert months(date(2026, 1, 30), date(2027, 2, 1), date(2027, 2, 28)) == [
        ("2027-02", date(2027, 2, 28))
    ]


def test_range_is_inclusive_and_ordered() -> None:
    dues = months(date(2026, 10, 5), date(2026, 10, 5), date(2026, 12, 5))
    assert [d for _, d in dues] == [date(2026, 10, 5), date(2026, 11, 5), date(2026, 12, 5)]
    assert months(date(2026, 10, 5), date(2026, 10, 6), date(2026, 11, 4)) == []


def test_annual_feb_29_anchor() -> None:
    dues = list(
        due_dates(BillingFrequency.ANNUAL, date(2028, 2, 29), date(2027, 1, 1), date(2029, 12, 31))
    )
    assert dues == [
        ("2027", date(2027, 2, 28)),
        ("2028", date(2028, 2, 29)),
        ("2029", date(2029, 2, 28)),
    ]


def test_local_midnight_is_converted_to_utc() -> None:
    assert local_instant(date(2026, 10, 5), datetime.min.time(), IST).isoformat() == (
        "2026-10-04T18:30:00+00:00"
    )


def test_seeded_scenarios_are_deterministic_and_known() -> None:
    first = [seeded_scenario_id(7, "SUB-2001", f"2026-{m:02d}") for m in range(1, 13)]
    again = [seeded_scenario_id(7, "SUB-2001", f"2026-{m:02d}") for m in range(1, 13)]
    assert first == again
    assert set(first) <= set(CATALOG)
    assert first != [seeded_scenario_id(8, "SUB-2001", f"2026-{m:02d}") for m in range(1, 13)]


def test_scenario_attempt_outcomes() -> None:
    recovers = CATALOG["INSUFFICIENT_FUNDS_RECOVERS"]
    assert [recovers.outcome_for_attempt(n).value for n in (1, 2, 3)] == [
        "failed",
        "succeeded",
        "succeeded",
    ]
    assert CATALOG["BANK_TIMEOUT"].outcome_for_attempt(1).value == "unknown"
    assert CATALOG["MANDATE_INACTIVE"].retry_permitted is False


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        (I.SCHEDULED, I.PROCESSING, True),
        (I.PROCESSING, I.PENDING_RECONCILIATION, True),
        (I.FAILED, I.RETRY_SCHEDULED, True),
        (I.RETRY_SCHEDULED, I.SUCCESS, True),
        (I.SUCCESS, I.FAILED, False),
        (I.SCHEDULED, I.SUCCESS, False),
        (I.PENDING_RECONCILIATION, I.RETRY_SCHEDULED, False),
    ],
)
def test_installment_state_machine(current: I, target: I, ok: bool) -> None:
    if ok:
        assert installment_transition(current, target) == target
    else:
        with pytest.raises(InvalidTransitionError):
            installment_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        (R.OPEN, R.ELIGIBLE, True),
        (R.ELIGIBLE, R.AWAITING_APPROVAL, True),
        (R.AWAITING_APPROVAL, R.APPROVED, True),
        (R.APPROVED, R.CALL_INITIATION_UNKNOWN, True),
        (R.CALL_FAILED, R.APPROVED, True),
        (R.OPEN, R.APPROVED, False),
        (R.CANCELLED, R.AWAITING_APPROVAL, False),
        (R.OPTED_OUT, R.APPROVED, False),
        (R.CALL_COMPLETED, R.CALL_INITIATED, False),
    ],
)
def test_recovery_state_machine(current: R, target: R, ok: bool) -> None:
    if ok:
        assert case_transition(current, target) == target
    else:
        with pytest.raises(InvalidTransitionError):
            case_transition(current, target)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Weekdays 6 PM - 8 PM IST", ({0, 1, 2, 3, 4}, "18:00", "20:00")),
        ("Weekdays 12 PM - 2 PM IST", ({0, 1, 2, 3, 4}, "12:00", "14:00")),
        ("Any day 11 AM - 1 PM IST", (set(range(7)), "11:00", "13:00")),
        ("Weekends 10 AM - 1 PM IST", ({5, 6}, "10:00", "13:00")),
        ("Weekdays after 7 PM IST", ({0, 1, 2, 3, 4}, "19:00", "20:00")),
        ("Sometime soon", None),
        ("Weekdays 8 PM - 6 PM IST", None),
    ],
)
def test_contact_window_parsing(text: str, expected: tuple | None) -> None:
    window = parse_contact_window(text, IST, SETTINGS)
    if expected is None:
        assert window is None
    else:
        assert window is not None
        assert (
            set(window.days),
            window.start.strftime("%H:%M"),
            window.end.strftime("%H:%M"),
        ) == expected


def customer(contact_time: str, consent: str = "synthetic_test_record") -> SimpleNamespace:
    return SimpleNamespace(preferred_contact_time=contact_time, customer_contact_consent=consent)


def test_contact_window_evaluated_in_virtual_local_time() -> None:
    c = customer("Weekdays 6 PM - 8 PM IST")
    assert (
        contact_window_denial(c, datetime(2026, 10, 14, 18, 30, tzinfo=IST), IST, SETTINGS) is None
    )  # type: ignore[arg-type]
    late = contact_window_denial(c, datetime(2026, 10, 14, 20, 0, tzinfo=IST), IST, SETTINGS)  # type: ignore[arg-type]
    saturday = contact_window_denial(c, datetime(2026, 10, 17, 18, 30, tzinfo=IST), IST, SETTINGS)  # type: ignore[arg-type]
    assert late is not None and saturday is not None and late.code == "outside_contact_window"
    unknown = contact_window_denial(
        customer("whenever"), datetime(2026, 10, 14, 12, tzinfo=IST), IST, SETTINGS
    )  # type: ignore[arg-type]
    assert unknown is not None and unknown.code == "contact_window_unknown"


def test_consent_and_destination_rules() -> None:
    assert consent_denial(customer("x"), SETTINGS) is None  # type: ignore[arg-type]
    strict = Settings(_env_file=None, allow_synthetic_consent_for_test_destinations=False)
    denial = consent_denial(customer("x"), strict)  # type: ignore[arg-type]
    assert denial is not None and denial.code == "consent_invalid"
    assert destination_denial("+15005550006", SETTINGS) is None
    blocked = destination_denial("+919000001001", SETTINGS)  # a synthetic customer number
    assert blocked is not None and blocked.code == "destination_not_allowlisted"
    with pytest.raises(ValueError, match=r"E\.164"):
        _ = Settings(_env_file=None, test_destination_allowlist="12345").allowed_test_destinations
