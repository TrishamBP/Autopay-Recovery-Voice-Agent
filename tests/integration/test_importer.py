from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.models import AuditLog
from src.customer_notes.models import CustomerNote
from src.customers.models import Customer
from src.evaluation.models import CallScenario
from src.importers.excel_importer import ImportOptions, run_import
from src.payment_history.models import PaymentHistory
from src.payment_history.service import stored_summary
from tests.factories import NOTE_A1, NOTE_B1, NOTE_B2, SUMMARY_B, base_sheets, write_workbook

Factory = async_sessionmaker[AsyncSession]


async def counts(factory: Factory) -> dict[str, int]:
    async with factory() as s:
        return {
            m.__tablename__: await s.scalar(select(func.count()).select_from(m)) or 0
            for m in (Customer, PaymentHistory, CustomerNote, CallScenario, AuditLog)
        }


async def test_import_inserts_everything_and_recomputes_summaries(
    session_factory: Factory, workbook: Path
) -> None:
    report = await run_import(session_factory, workbook, ImportOptions())
    assert report.outcome == "imported"
    assert await counts(session_factory) == {
        "customer": 2,
        "payment_history": 12,
        "customer_note": 3,
        "call_scenario": 2,
        "audit_log": 1,
    }
    async with session_factory() as s:
        b = await s.get(Customer, "CUS-1002")
        assert b is not None
        assert b.payment_amount_paise == 49900
        assert b.do_not_contact is False
        summary = stored_summary(b)
        assert summary.payment_history_summary == SUMMARY_B
        assert summary.minimum_amount_paid_6m == Decimal("210.00")
        assert summary.total_amount_paid_6m == Decimal("1707.00")
        assert summary.payment_reliability_score == 50
        assert b.customer_notes == f"{NOTE_B1} | {NOTE_B2}"
        a = await s.get(Customer, "CUS-1001")
        assert a is not None and a.customer_notes == NOTE_A1


async def test_import_is_idempotent(session_factory: Factory, workbook: Path) -> None:
    await run_import(session_factory, workbook, ImportOptions())
    before = await counts(session_factory)
    second = await run_import(session_factory, workbook, ImportOptions())
    assert second.outcome == "no_changes"
    assert all(p.counts()["insert"] == 0 for p in second.plan.values())
    assert await counts(session_factory) == before  # no duplicates, no extra audit rows


async def test_dry_run_writes_nothing(session_factory: Factory, workbook: Path) -> None:
    report = await run_import(session_factory, workbook, ImportOptions(dry_run=True))
    assert report.outcome == "dry_run"
    assert report.plan["customer"].counts()["insert"] == 2
    assert set((await counts(session_factory)).values()) == {0}


async def test_changed_record_is_a_conflict_and_nothing_is_written(
    session_factory: Factory, tmp_path: Path, workbook: Path
) -> None:
    await run_import(session_factory, workbook, ImportOptions())
    sheets = base_sheets()
    sheets["Customers"][0]["preferred_contact_time"] = "Any day 9 AM - 10 AM IST"
    later = datetime(2026, 9, 1, 10, 0)
    sheets["Customer_Notes"].append(
        {
            **sheets["Customer_Notes"][0],
            "note_id": "NOTE-0004",
            "note_text": "New note.",
            "created_at": later,
            "updated_at": later,
        }
    )
    sheets["Customers"][0]["customer_notes"] = f"{NOTE_A1} | New note."
    sheets["Customers"][0]["notes_updated_at"] = later
    changed = write_workbook(tmp_path / "changed.xlsx", sheets)

    report = await run_import(session_factory, changed, ImportOptions())
    assert report.outcome == "blocked", report.to_dict()
    assert report.validation.discrepancies == []
    assert report.plan["customer_note"].counts()["insert"] == 1  # planned, but not applied
    assert [c.fields for c in report.plan["customer"].conflicts] == [["preferred_contact_time"]]
    async with session_factory() as s:
        assert await s.get(CustomerNote, "NOTE-0004") is None  # the new note was NOT inserted


async def test_allow_updates_applies_and_audits_but_never_reverts_opt_out(
    session_factory: Factory, tmp_path: Path, workbook: Path
) -> None:
    await run_import(session_factory, workbook, ImportOptions())
    async with session_factory() as s, s.begin():
        b = await s.get(Customer, "CUS-1002")
        assert b is not None
        b.do_not_contact = True  # opted out at runtime
    sheets = base_sheets()
    sheets["Customers"][0]["preferred_contact_time"] = "Any day 9 AM - 10 AM IST"
    changed = write_workbook(tmp_path / "changed.xlsx", sheets)

    report = await run_import(session_factory, changed, ImportOptions(allow_updates=True))
    assert report.outcome == "imported"
    assert [key for key, _ in report.plan["customer"].updates] == ["CUS-1001"]
    assert [c.key for c in report.plan["customer"].rejected] == ["CUS-1002"]
    async with session_factory() as s:
        a, b = await s.get(Customer, "CUS-1001"), await s.get(Customer, "CUS-1002")
        assert a is not None and a.preferred_contact_time == "Any day 9 AM - 10 AM IST"
        assert b is not None and b.do_not_contact is True
        events = (await s.scalars(select(AuditLog.event_type))).all()
        assert "import.record_updated" in events


async def test_summary_discrepancy_blocks_unless_accepted(
    session_factory: Factory, tmp_path: Path
) -> None:
    sheets = base_sheets()
    sheets["Customers"][0]["payment_reliability_score"] = 99
    path = write_workbook(tmp_path / "bad_summary.xlsx", sheets)

    blocked = await run_import(session_factory, path, ImportOptions())
    assert blocked.outcome == "blocked"
    assert set((await counts(session_factory)).values()) == {0}

    accepted = await run_import(
        session_factory, path, ImportOptions(accept_computed_summaries=True)
    )
    assert accepted.outcome == "imported"
    async with session_factory() as s:
        a = await s.get(Customer, "CUS-1001")
        assert a is not None and a.payment_reliability_score == 83  # recomputed, not 99


async def test_invalid_workbook_writes_nothing(session_factory: Factory, tmp_path: Path) -> None:
    sheets = base_sheets()
    sheets["Payment_History"][0]["customer_id"] = "CUS-4040"
    report = await run_import(
        session_factory, write_workbook(tmp_path / "x.xlsx", sheets), ImportOptions()
    )
    assert report.outcome == "validation_failed"
    assert set((await counts(session_factory)).values()) == {0}


async def test_database_constraint_violation_rolls_back(
    session_factory: Factory, tmp_path: Path, workbook: Path
) -> None:
    await run_import(session_factory, workbook, ImportOptions())
    sheets = base_sheets()
    third = {
        **sheets["Customers"][0],
        "customer_id": "CUS-1003",
        "subscription_id": "SUB-2003",
        "email": "third@example.com",
    }  # duplicate phone number of CUS-1001
    sheets["Customers"].append(third)
    sheets["Payment_History"] += [
        {**h, "history_id": f"HIST-{100 + i:04d}", "customer_id": "CUS-1003"}
        for i, h in enumerate(sheets["Payment_History"][:6])
    ]
    path = write_workbook(tmp_path / "dupe_phone.xlsx", sheets)
    report = await run_import(session_factory, path, ImportOptions())
    # Workbook-level validation catches the duplicate before the database does.
    assert report.outcome == "validation_failed"
    assert "duplicate phone_number" in " ".join(e.message for e in report.validation.errors)
    assert (await counts(session_factory))["customer"] == 2
