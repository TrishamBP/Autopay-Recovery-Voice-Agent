from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.importers.validation import ValidationReport, validate_workbook
from src.importers.workbook import read_workbook
from tests.factories import base_sheets, write_workbook


def validate(tmp_path: Path, sheets: dict) -> ValidationReport:
    report, _ = validate_workbook(read_workbook(write_workbook(tmp_path / "wb.xlsx", sheets)))
    return report


def messages(report: ValidationReport) -> str:
    return " | ".join(f"{e.sheet}:{e.column or ''}:{e.message}" for e in report.errors)


def test_fixture_workbook_is_valid_and_consistent(tmp_path: Path) -> None:
    report, data = validate_workbook(read_workbook(write_workbook(tmp_path / "wb.xlsx")))
    assert report.ok, messages(report)
    assert report.discrepancies == []
    assert data is not None
    assert report.rows_read == {
        "Customers": 2,
        "Payment_History": 12,
        "Customer_Notes": 3,
        "Call_Scenarios": 2,
        "Enums": sum(1 for _ in base_sheets()["Enums"]),
    }
    # IST -> UTC conversion (11:27 IST == 05:57 UTC)
    assert data.customers[0].last_payment_attempt_at == datetime(2026, 10, 5, 5, 57, tzinfo=UTC)


def test_missing_worksheet(tmp_path: Path) -> None:
    sheets = base_sheets()
    del sheets["Customer_Notes"]
    assert "Customer_Notes::required worksheet is missing" in messages(validate(tmp_path, sheets))


def test_missing_and_unexpected_columns(tmp_path: Path) -> None:
    sheets = base_sheets()
    for row in sheets["Customers"]:
        del row["email"]
        row["mystery"] = 1
    text = messages(validate(tmp_path, sheets))
    assert "Customers:email:required column is missing" in text
    assert "Customers:mystery:unexpected column" in text


def test_duplicate_customer_ids(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Customers"][1]["customer_id"] = "CUS-1001"
    assert "duplicate customer_id 'CUS-1001'" in messages(validate(tmp_path, sheets))


def test_duplicate_customer_month(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Payment_History"][1]["billing_month"] = "2026-05"
    sheets["Payment_History"][1]["due_date"] = datetime(2026, 5, 5)
    sheets["Payment_History"][1]["payment_date"] = datetime(2026, 5, 5)
    assert "duplicate (customer_id, billing_month) 'CUS-1001/2026-05'" in messages(
        validate(tmp_path, sheets)
    )


@pytest.mark.parametrize(
    ("sheet", "column", "value", "expected"),
    [
        ("Payment_History", "payment_status", "paid_maybe", "payment_status"),
        ("Customers", "payment_status", "PAID!!", "payment_status"),
        ("Customers", "payment_due_date", "05/10/2026", "payment_due_date"),
        ("Customers", "payment_due_date", datetime(2026, 10, 5, 13, 30), "time component"),
        ("Customers", "payment_amount", "₹abc", "not an amount"),
        ("Customers", "payment_amount", -5, "greater than 0"),
        ("Customers", "do_not_contact", "maybe", "TRUE/FALSE"),
        ("Customers", "full_name", None, "full_name"),
        ("Customers", "phone_number", "12345", "E.164"),
        ("Customers", "email", "real.person@gmail.com", "email"),
        ("Customer_Notes", "note_type", "gossip", "note_type"),
        ("Customer_Notes", "note_text", "Customer read out OTP 123456 on the call", "credentials"),
    ],
)
def test_invalid_cell_values(
    tmp_path: Path, sheet: str, column: str, value: object, expected: str
) -> None:
    sheets = base_sheets()
    sheets[sheet][0][column] = value
    assert expected in messages(validate(tmp_path, sheets))


def test_history_semantic_rules(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Payment_History"][0]["amount_paid"] = 900  # > amount_due
    sheets["Payment_History"][3]["payment_status"] = "missed"  # paid amount on a missed cycle
    text = messages(validate(tmp_path, sheets))
    assert "amount_paid exceeds amount_due" in text
    assert "missed cycle must have amount_paid 0" in text


def test_foreign_key_validation(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Customer_Notes"][0]["customer_id"] = "CUS-9999"
    assert "customer_id 'CUS-9999' not found in Customers" in messages(validate(tmp_path, sheets))


def test_current_charge_must_mirror_latest_cycle(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Customers"][0]["payment_amount"] = 899
    assert "does not match latest cycle" in messages(validate(tmp_path, sheets))


def test_enum_sheet_must_match_backend(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Enums"].append(
        {"enum_name": "payment_status", "value": "refunded", "description": "d", "used_in": "x"}
    )
    assert "enum 'payment_status' differs from backend" in messages(validate(tmp_path, sheets))


def test_conflicting_precomputed_summaries_are_reported_not_rewritten(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Customers"][0]["payments_on_time_6m"] = 6
    sheets["Customers"][0]["payment_reliability_score"] = 99
    sheets["Customers"][1]["customer_notes"] = "tampered digest"
    report = validate(tmp_path, sheets)
    assert report.ok  # discrepancies are not row errors...
    found = {
        (d.customer_id, d.field): (d.workbook_value, d.computed_value) for d in report.discrepancies
    }
    assert found[("CUS-1001", "payments_on_time_6m")] == ("6", "5")
    assert found[("CUS-1001", "payment_reliability_score")] == ("99", "83")
    assert ("CUS-1002", "customer_notes") in found


def test_currency_text_cells_are_parsed(tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Customers"][0]["payment_amount"] = "₹799.00"
    sheets["Customers"][0]["total_amount_paid_6m"] = "3,995.00"
    report = validate(tmp_path, sheets)
    assert report.ok, messages(report)
    assert report.discrepancies == []
