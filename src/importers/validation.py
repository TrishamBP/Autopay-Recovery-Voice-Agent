"""Stages 2, 5 and 6: structure checks, per-row validation, cross-record checks, report.

Nothing here touches the database. Output is a `ValidatedWorkbook` (normalised rows) plus a
`ValidationReport`. Any `errors` make the workbook unimportable. `discrepancies` (workbook
precomputed values that disagree with values recomputed from canonical rows) also block the
import unless the caller explicitly accepts the recomputed values.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from src.common.enums import WORKBOOK_ENUMS, PaymentHistoryStatus, PaymentStatus
from src.customer_notes.service import DIGEST_SEPARATOR
from src.importers.schemas import (
    CallScenarioImport,
    CustomerImport,
    CustomerNoteImport,
    ImportRow,
    PaymentHistoryImport,
)
from src.importers.workbook import RawSheet, RawWorkbook
from src.payment_history.summary import CycleFacts, HistorySummary, compute_summary

REQUIRED_SHEETS = ("Customers", "Payment_History", "Customer_Notes", "Call_Scenarios", "Enums")
OPTIONAL_SHEETS = ("README",)  # documentation only; never imported
ENUM_SHEET_COLUMNS = ("enum_name", "value", "description", "used_in")
SUMMARY_FIELDS = tuple(HistorySummary.__dataclass_fields__)


@dataclass(frozen=True)
class Issue:
    sheet: str
    message: str
    row: int | None = None
    column: str | None = None


@dataclass(frozen=True)
class Discrepancy:
    customer_id: str
    field: str
    workbook_value: str | None
    computed_value: str | None


@dataclass
class ValidationReport:
    workbook_path: str
    workbook_sha256: str
    sheets_found: list[str]
    rows_read: dict[str, int] = field(default_factory=dict)
    blank_rows_skipped: dict[str, int] = field(default_factory=dict)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"ok": self.ok}


@dataclass
class ValidatedWorkbook:
    customers: list[CustomerImport]
    history: list[PaymentHistoryImport]
    notes: list[CustomerNoteImport]
    scenarios: list[CallScenarioImport]
    summaries: dict[str, HistorySummary]  # recomputed from history, keyed by customer_id


def _fmt(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _check_headers(sheet: RawSheet, expected: list[str], report: ValidationReport) -> bool:
    dupes = [h for h, n in Counter(sheet.headers).items() if n > 1]
    missing = [c for c in expected if c not in sheet.headers]
    unexpected = [h for h in sheet.headers if h and h not in expected]
    for column in dupes:
        report.errors.append(Issue(sheet.name, "duplicate column header", column=column))
    for column in missing:
        report.errors.append(Issue(sheet.name, "required column is missing", column=column))
    for column in unexpected:
        # An unmapped column would be dropped silently; refuse instead.
        report.errors.append(Issue(sheet.name, "unexpected column (not mapped)", column=column))
    return not (dupes or missing or unexpected)


def _parse_sheet[M: ImportRow](
    sheet: RawSheet, model: type[M], report: ValidationReport
) -> list[M]:
    report.rows_read[sheet.name] = len(sheet.rows)
    report.blank_rows_skipped[sheet.name] = sheet.blank_rows_skipped
    if not _check_headers(sheet, list(model.model_fields), report):
        return []
    parsed: list[M] = []
    for raw in sheet.rows:
        try:
            parsed.append(model.model_validate(raw.values))
        except ValidationError as exc:
            for err in exc.errors(include_url=False, include_input=False):
                column = ".".join(str(p) for p in err["loc"]) or None
                report.errors.append(
                    Issue(sheet.name, err["msg"], row=raw.row_number, column=column)
                )
    return parsed


def _unique(sheet: str, label: str, values: list[str], report: ValidationReport) -> None:
    for value, count in Counter(values).items():
        if count > 1:
            report.errors.append(Issue(sheet, f"duplicate {label} {value!r} ({count} rows)"))


def _check_enums(sheet: RawSheet, report: ValidationReport) -> None:
    if not _check_headers(sheet, list(ENUM_SHEET_COLUMNS), report):
        return
    sheet_values: dict[str, list[str]] = defaultdict(list)
    for raw in sheet.rows:
        sheet_values[str(raw.values.get("enum_name"))].append(str(raw.values.get("value")))
    for name, enum_cls in WORKBOOK_ENUMS.items():
        expected = [m.value for m in enum_cls]
        if sorted(sheet_values.get(name, [])) != sorted(expected):
            report.errors.append(
                Issue(
                    "Enums",
                    f"enum {name!r} differs from backend: sheet={sheet_values.get(name)} "
                    f"backend={expected}",
                )
            )
    for name in sorted(set(sheet_values) - set(WORKBOOK_ENUMS)):
        report.errors.append(
            Issue("Enums", f"unknown enum {name!r} is not supported by the backend")
        )


def _cross_checks(data: ValidatedWorkbook, report: ValidationReport) -> None:
    customers = {c.customer_id: c for c in data.customers}
    _unique("Customers", "customer_id", [c.customer_id for c in data.customers], report)
    _unique("Customers", "subscription_id", [c.subscription_id for c in data.customers], report)
    _unique("Customers", "phone_number", [c.phone_number for c in data.customers], report)
    _unique("Customers", "email", [c.email for c in data.customers], report)
    _unique("Payment_History", "history_id", [h.history_id for h in data.history], report)
    _unique(
        "Payment_History",
        "(customer_id, billing_month)",
        [f"{h.customer_id}/{h.billing_month}" for h in data.history],
        report,
    )
    _unique("Customer_Notes", "note_id", [n.note_id for n in data.notes], report)
    _unique("Call_Scenarios", "customer_id", [s.customer_id for s in data.scenarios], report)
    _unique("Call_Scenarios", "scenario_name", [s.scenario_name for s in data.scenarios], report)

    references = {
        "Payment_History": [h.customer_id for h in data.history],
        "Customer_Notes": [n.customer_id for n in data.notes],
        "Call_Scenarios": [s.customer_id for s in data.scenarios],
    }
    for sheet, customer_ids in references.items():
        for cid in sorted(set(customer_ids) - set(customers)):
            report.errors.append(Issue(sheet, f"customer_id {cid!r} not found in Customers"))

    by_customer: dict[str, list[PaymentHistoryImport]] = defaultdict(list)
    for h in data.history:
        by_customer[h.customer_id].append(h)
    for cid, customer in customers.items():
        cycles = sorted(by_customer.get(cid, []), key=lambda h: h.billing_month)
        if not cycles:
            report.warnings.append(Issue("Payment_History", f"{cid} has no billing cycles"))
            continue
        latest = cycles[-1]
        for h in cycles[:-1]:
            if h.payment_status == PaymentHistoryStatus.FAILED:
                report.errors.append(
                    Issue(
                        "Payment_History",
                        f"{h.history_id}: 'failed' is only "
                        "allowed for the latest (open) cycle; use 'missed'",
                    )
                )
        if latest.payment_status == PaymentHistoryStatus.MISSED:
            report.errors.append(
                Issue("Payment_History", f"{latest.history_id}: the open cycle cannot be 'missed'")
            )
        if customer.payment_status == PaymentStatus.FAILED:
            mirror = {
                "payment_amount": (customer.payment_amount, latest.amount_due),
                "payment_due_date": (customer.payment_due_date, latest.due_date),
                "payment_failure_reason": (customer.payment_failure_reason, latest.failure_reason),
                "payment_attempt_count": (customer.payment_attempt_count, latest.retry_count + 1),
                "latest cycle status": (PaymentHistoryStatus.FAILED, latest.payment_status),
            }
            for label, (current, cycle_value) in mirror.items():
                if current != cycle_value:
                    report.errors.append(
                        Issue(
                            "Customers",
                            f"{cid}: {label}={_fmt(current)} does "
                            f"not match latest cycle ({_fmt(cycle_value)})",
                        )
                    )


def _summary_discrepancies(data: ValidatedWorkbook, report: ValidationReport) -> None:
    notes_by_customer: dict[str, list[CustomerNoteImport]] = defaultdict(list)
    for n in data.notes:
        notes_by_customer[n.customer_id].append(n)
    for c in data.customers:
        computed = data.summaries[c.customer_id]
        for name in SUMMARY_FIELDS:
            workbook_value, computed_value = getattr(c, name), getattr(computed, name)
            if workbook_value != computed_value:
                report.discrepancies.append(
                    Discrepancy(c.customer_id, name, _fmt(workbook_value), _fmt(computed_value))
                )
        own = sorted(notes_by_customer[c.customer_id], key=lambda n: (n.created_at, n.note_id))
        digest = DIGEST_SEPARATOR.join(n.note_text for n in own)
        latest = max((n.updated_at for n in own), default=None)
        if (c.customer_notes or "") != digest:
            report.discrepancies.append(
                Discrepancy(c.customer_id, "customer_notes", c.customer_notes, digest)
            )
        if c.notes_updated_at != latest:
            report.discrepancies.append(
                Discrepancy(
                    c.customer_id, "notes_updated_at", _fmt(c.notes_updated_at), _fmt(latest)
                )
            )


def validate_workbook(raw: RawWorkbook) -> tuple[ValidationReport, ValidatedWorkbook | None]:
    report = ValidationReport(raw.path, raw.sha256, raw.sheet_names)
    for name in REQUIRED_SHEETS:
        if name not in raw.sheets:
            report.errors.append(Issue(name, "required worksheet is missing"))
    for name in raw.sheet_names:
        if name not in REQUIRED_SHEETS and name not in OPTIONAL_SHEETS:
            report.warnings.append(Issue(name, "unrecognised worksheet ignored"))
    if report.errors:
        return report, None

    customers = _parse_sheet(raw.sheets["Customers"], CustomerImport, report)
    history = _parse_sheet(raw.sheets["Payment_History"], PaymentHistoryImport, report)
    notes = _parse_sheet(raw.sheets["Customer_Notes"], CustomerNoteImport, report)
    scenarios = _parse_sheet(raw.sheets["Call_Scenarios"], CallScenarioImport, report)
    _check_enums(raw.sheets["Enums"], report)
    report.rows_read["Enums"] = len(raw.sheets["Enums"].rows)
    if report.errors:
        return report, None

    cycles: dict[str, list[CycleFacts]] = defaultdict(list)
    for h in history:
        cycles[h.customer_id].append(
            CycleFacts(h.billing_month, h.payment_status, h.amount_paid, h.recovered_after_failure)
        )
    data = ValidatedWorkbook(
        customers=customers,
        history=history,
        notes=notes,
        scenarios=scenarios,
        summaries={c.customer_id: compute_summary(cycles[c.customer_id]) for c in customers},
    )
    _cross_checks(data, report)
    if report.errors:
        return report, None
    _summary_discrepancies(data, report)
    return report, data
