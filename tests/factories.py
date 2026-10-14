"""Deterministic fixture workbook (2 customers) with HAND-COMPUTED summaries.

Customer A (CUS-1001): five on-time cycles of 799, open October failure.
Customer B (CUS-1002): on time, late+recovered, partial (210), missed, on time, open failure.
Expected summaries are written out literally so the summary calculator is not testing itself.
"""

import copy
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from src.common.enums import WORKBOOK_ENUMS

MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]

SUMMARY_A = (
    "May 2026 to Oct 2026: 5 on time, 0 late, 0 partially paid, 0 missed, 1 failed (open). "
    "0 cycles recovered after an autopay failure. Total received INR 3,995.00."
)
SUMMARY_B = (
    "May 2026 to Oct 2026: 2 on time, 1 late, 1 partially paid, 1 missed, 1 failed (open). "
    "1 cycle recovered after an autopay failure. Total received INR 1,707.00."
)
NOTE_A1 = "Customer prefers calls on weekday evenings."
NOTE_B1 = "Customer previously requested a reminder before the due date."
NOTE_B2 = "Card autopay was declined; customer paid via UPI after a reminder."


def _customer(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "customer_id": "CUS-1001",
        "full_name": "Aarav Mehta",
        "phone_number": "+919000001001",
        "email": "aarav.mehta@example.com",
        "subscription_id": "SUB-2001",
        "subscription_plan": "Premium Monthly",
        "payment_amount": 799,
        "currency": "INR",
        "billing_frequency": "monthly",
        "payment_due_date": datetime(2026, 10, 5),
        "payment_failure_date": datetime(2026, 10, 5),
        "payment_failure_reason": "mandate_inactive",
        "payment_status": "failed",
        "payment_attempt_count": 1,
        "last_payment_attempt_at": datetime(2026, 10, 5, 11, 27),
        "next_retry_eligible_at": None,
        "payment_link_status": "not_created",
        "payment_link_url": None,
        "customer_contact_consent": "synthetic_test_record",
        "preferred_language": "English",
        "preferred_contact_time": "Weekdays 6 PM - 8 PM IST",
        "recovery_status": "pending",
        "callback_requested_at": None,
        "do_not_contact": False,
        "last_call_id": None,
        "last_call_status": "not_called",
        "last_call_outcome": "not_attempted",
        "updated_at": datetime(2026, 10, 5, 11, 40),
        "payment_history_summary": SUMMARY_A,
        "payments_on_time_6m": 5,
        "payments_late_6m": 0,
        "payments_failed_6m": 1,
        "payments_missed_6m": 0,
        "minimum_amount_paid_6m": 799,
        "total_amount_paid_6m": 3995,
        "payment_reliability_score": 83,
        "customer_notes": NOTE_A1,
        "notes_updated_at": datetime(2026, 6, 14, 18, 20),
    }
    return base | overrides


def _cycle(cid: str, n: int, month: str, day: int, due: int, **kw: object) -> dict[str, object]:
    year, mon = map(int, month.split("-"))
    row: dict[str, object] = {
        "history_id": f"HIST-{n:04d}",
        "customer_id": cid,
        "billing_month": month,
        "due_date": datetime(year, mon, day),
        "amount_due": due,
        "amount_paid": due,
        "payment_status": "paid_on_time",
        "payment_date": datetime(year, mon, day),
        "days_late": 0,
        "minimum_amount_paid": False,
        "payment_method_type": "e-mandate",
        "failure_reason": None,
        "retry_count": 0,
        "recovered_after_failure": False,
        "notes": None,
    }
    return row | kw


def _open(cid: str, n: int, day: int, due: int, reason: str, retries: int = 0) -> dict[str, object]:
    return _cycle(
        cid,
        n,
        "2026-10",
        day,
        due,
        amount_paid=0,
        payment_status="failed",
        payment_date=None,
        days_late=None,
        failure_reason=reason,
        retry_count=retries,
    )


def base_sheets() -> dict[str, list[dict[str, object]]]:
    a = [_cycle("CUS-1001", i + 1, m, 5, 799) for i, m in enumerate(MONTHS[:5])]
    a.append(_open("CUS-1001", 6, 5, 799, "mandate_inactive"))
    b = [
        _cycle("CUS-1002", 7, "2026-05", 3, 499, payment_method_type="card"),
        _cycle(
            "CUS-1002",
            8,
            "2026-06",
            3,
            499,
            payment_status="paid_late",
            payment_date=datetime(2026, 6, 7),
            days_late=4,
            payment_method_type="UPI",
            failure_reason="bank_declined",
            recovered_after_failure=True,
            notes="Autopay failed (bank_declined); paid via UPI 4 days late.",
        ),
        _cycle(
            "CUS-1002",
            9,
            "2026-07",
            3,
            499,
            payment_status="partially_paid",
            amount_paid=210,
            payment_date=datetime(2026, 7, 6),
            days_late=3,
            minimum_amount_paid=True,
            payment_method_type="UPI",
            failure_reason="insufficient_funds",
            retry_count=1,
            notes="Partial payment.",
        ),
        _cycle(
            "CUS-1002",
            10,
            "2026-08",
            3,
            499,
            payment_status="missed",
            amount_paid=0,
            payment_date=None,
            days_late=None,
            payment_method_type="card",
            failure_reason="bank_declined",
            retry_count=2,
            notes="Missed.",
        ),
        _cycle("CUS-1002", 11, "2026-09", 3, 499, payment_method_type="card"),
        _open("CUS-1002", 12, 3, 499, "expired_card") | {"payment_method_type": "card"},
    ]
    customers = [
        _customer(),
        _customer(
            customer_id="CUS-1002",
            full_name="Priya Raghavan",
            phone_number="+919000001002",
            email="priya.raghavan@example.com",
            subscription_id="SUB-2002",
            subscription_plan="Standard Monthly",
            payment_amount=499,
            payment_due_date=datetime(2026, 10, 3),
            payment_failure_date=datetime(2026, 10, 3),
            payment_failure_reason="expired_card",
            last_payment_attempt_at=datetime(2026, 10, 3, 11, 36),
            preferred_contact_time="Weekdays 12 PM - 2 PM IST",
            updated_at=datetime(2026, 10, 3, 11, 38),
            payment_history_summary=SUMMARY_B,
            payments_on_time_6m=2,
            payments_late_6m=1,
            payments_failed_6m=1,
            payments_missed_6m=1,
            minimum_amount_paid_6m=210,
            total_amount_paid_6m=1707,
            payment_reliability_score=50,
            customer_notes=f"{NOTE_B1} | {NOTE_B2}",
            notes_updated_at=datetime(2026, 9, 11, 9, 0),
        ),
    ]
    notes = [
        {
            "note_id": "NOTE-0001",
            "customer_id": "CUS-1001",
            "created_at": datetime(2026, 6, 14, 18, 20),
            "note_type": "customer_preference",
            "note_text": NOTE_A1,
            "created_by": "seed_data",
            "source_call_id": None,
            "updated_at": datetime(2026, 6, 14, 18, 20),
        },
        {
            "note_id": "NOTE-0002",
            "customer_id": "CUS-1002",
            "created_at": datetime(2026, 7, 2, 13, 5),
            "note_type": "customer_preference",
            "note_text": NOTE_B1,
            "created_by": "seed_data",
            "source_call_id": None,
            "updated_at": datetime(2026, 7, 2, 13, 5),
        },
        {
            "note_id": "NOTE-0003",
            "customer_id": "CUS-1002",
            "created_at": datetime(2026, 9, 10, 12, 30),
            "note_type": "payment_context",
            "note_text": NOTE_B2,
            "created_by": "seed_data",
            "source_call_id": None,
            "updated_at": datetime(2026, 9, 11, 9, 0),
        },
    ]
    scenarios = [
        {
            "customer_id": cid,
            "scenario_name": f"Scenario {cid}",
            "customer_persona": "Persona",
            "likely_opening_response": "Hello",
            "likely_customer_intent": intent,
            "expected_agent_behavior": "Behave",
            "expected_tool_action": tool,
            "expected_final_outcome": "payment_status=failed",
            "evaluation_notes": "Notes",
        }
        for cid, intent, tool in [
            ("CUS-1001", "agree_to_update_payment_method", "request_payment_method_update_link"),
            ("CUS-1002", "report_expired_card", "request_payment_method_update_link"),
        ]
    ]
    enums = [
        {"enum_name": name, "value": m.value, "description": "d", "used_in": "x"}
        for name, cls in WORKBOOK_ENUMS.items()
        for m in cls
    ]
    return {
        "Customers": customers,
        "Call_Scenarios": scenarios,
        "Payment_History": a + b,
        "Customer_Notes": notes,
        "Enums": enums,
        "README": [{"section": "Purpose", "details": "fixture"}],
    }


def write_workbook(path: Path, sheets: dict[str, list[dict[str, object]]] | None = None) -> Path:
    sheets = copy.deepcopy(sheets if sheets is not None else base_sheets())
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        headers = list(rows[0]) if rows else ["placeholder"]
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h) for h in headers])
    wb.save(path)
    return path
