"""Stages 7-9: plan against the database, apply in ONE transaction, recompute derived summaries.

Import behaviour (explicit, documented in docs/database.md):
- Records are matched by natural key (customer_id, history_id, note_id, call_scenario.customer_id).
- New key                 -> insert.
- Same key, same values   -> unchanged (skipped). Re-running with unchanged input writes nothing.
- Same key, different     -> CONFLICT. The whole import is refused, nothing is written.
  With `allow_updates=True` the differing fields are updated instead (audit-logged), EXCEPT
  `do_not_contact` true -> false, which is always rejected (opt-outs are one-way).
- Rows in the database but not in the workbook are never deleted; they are counted.
- Derived customer summaries are never taken from the workbook: they are recomputed from
  `payment_history` / `customer_note`. Workbook values that disagree are reported as
  discrepancies and block the import unless `accept_computed_summaries=True`.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.service import record_audit
from src.billing.models import Subscription
from src.common.enums import ActorType, PaymentStatus
from src.common.money import rupees_to_paise
from src.customer_notes.models import CustomerNote
from src.customer_notes.service import refresh_notes_digest
from src.customers.models import Customer
from src.database import Base
from src.evaluation.models import CallScenario
from src.importers.schemas import (
    CallScenarioImport,
    CustomerImport,
    CustomerNoteImport,
    PaymentHistoryImport,
)
from src.importers.validation import ValidatedWorkbook, ValidationReport, validate_workbook
from src.importers.workbook import read_workbook
from src.payment_history.models import PaymentHistory
from src.payment_history.service import refresh_customer_summary

logger = logging.getLogger(__name__)

Outcome = Literal["validation_failed", "blocked", "dry_run", "no_changes", "imported", "failed"]
PROTECTED_ONE_WAY = {"do_not_contact"}


@dataclass(frozen=True)
class ImportOptions:
    dry_run: bool = False
    allow_updates: bool = False
    accept_computed_summaries: bool = False


@dataclass(frozen=True)
class Conflict:
    key: str
    fields: list[str]


@dataclass
class TablePlan:
    table: str
    inserts: list[dict[str, object]] = field(default_factory=list)
    updates: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    unchanged: int = 0
    conflicts: list[Conflict] = field(default_factory=list)
    rejected: list[Conflict] = field(default_factory=list)
    not_in_workbook: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "insert": len(self.inserts),
            "update": len(self.updates),
            "unchanged": self.unchanged,
            "conflict": len(self.conflicts),
            "rejected": len(self.rejected),
            "not_in_workbook": self.not_in_workbook,
        }


@dataclass
class ImportReport:
    outcome: Outcome
    options: ImportOptions
    validation: ValidationReport
    plan: dict[str, TablePlan] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "options": asdict(self.options),
            "validation": self.validation.to_dict(),
            "plan": {
                name: p.counts()
                | {
                    "conflicts": [asdict(c) for c in p.conflicts],
                    "rejected": [asdict(c) for c in p.rejected],
                }
                for name, p in self.plan.items()
            },
            "messages": self.messages,
        }


# --- Row -> column mappings -------------------------------------------------------------------


def customer_columns(c: CustomerImport) -> dict[str, object]:
    """Workbook-sourced customer columns. Derived summary/digest columns are excluded: they are
    recomputed from canonical rows after the import."""
    return {
        "customer_id": c.customer_id,
        "full_name": c.full_name,
        "phone_number": c.phone_number,
        "email": c.email,
        "subscription_id": c.subscription_id,
        "subscription_plan": c.subscription_plan,
        "payment_amount_paise": rupees_to_paise(c.payment_amount),
        "currency": c.currency,
        "billing_frequency": c.billing_frequency,
        "payment_due_date": c.payment_due_date,
        "payment_failure_date": c.payment_failure_date,
        "payment_failure_reason": c.payment_failure_reason,
        "payment_status": c.payment_status,
        "payment_attempt_count": c.payment_attempt_count,
        "last_payment_attempt_at": c.last_payment_attempt_at,
        "next_retry_eligible_at": c.next_retry_eligible_at,
        "payment_link_status": c.payment_link_status,
        "payment_link_url": c.payment_link_url,
        "customer_contact_consent": c.customer_contact_consent,
        "preferred_language": c.preferred_language,
        "preferred_contact_time": c.preferred_contact_time,
        "recovery_status": c.recovery_status,
        "callback_requested_at": c.callback_requested_at,
        "do_not_contact": c.do_not_contact,
        "last_call_id": c.last_call_id,
        "last_call_status": c.last_call_status,
        "last_call_outcome": c.last_call_outcome,
        "updated_at": c.updated_at,
    }


def subscription_columns(c: CustomerImport) -> dict[str, object]:
    """One subscription per customer, derived from the workbook's subscription columns. The
    anchor is the current charge's due date (day-of-month rule for monthly billing)."""
    return {
        "subscription_id": c.subscription_id,
        "customer_id": c.customer_id,
        "billing_frequency": c.billing_frequency,
        "billing_anchor_date": c.payment_due_date,
        "billing_day_of_month": c.payment_due_date.day,
        "amount_minor_units": rupees_to_paise(c.payment_amount),
        "currency": c.currency,
        "is_active": c.payment_status != PaymentStatus.CANCELLED,
        "recovery_policy_id": "standard",
    }


def history_columns(h: PaymentHistoryImport) -> dict[str, object]:
    return {
        "history_id": h.history_id,
        "customer_id": h.customer_id,
        "billing_month": h.billing_month,
        "due_date": h.due_date,
        "amount_due_paise": rupees_to_paise(h.amount_due),
        "amount_paid_paise": rupees_to_paise(h.amount_paid),
        "status": h.payment_status,
        "payment_date": h.payment_date,
        "days_late": h.days_late,
        "minimum_amount_paid": h.minimum_amount_paid,
        "payment_method": h.payment_method_type,
        "failure_reason": h.failure_reason,
        "retry_count": h.retry_count,
        "recovered_after_failure": h.recovered_after_failure,
        "notes": h.notes,
    }


def note_columns(n: CustomerNoteImport) -> dict[str, object]:
    return n.model_dump()


def scenario_columns(s: CallScenarioImport) -> dict[str, object]:
    return s.model_dump()


# Table name -> (model, key column)
TABLES: dict[str, tuple[type[Base], str]] = {
    "customer": (Customer, "customer_id"),
    "subscription": (Subscription, "subscription_id"),
    "payment_history": (PaymentHistory, "history_id"),
    "customer_note": (CustomerNote, "note_id"),
    "call_scenario": (CallScenario, "customer_id"),
}


async def _plan_table(
    session: AsyncSession, table: str, rows: Sequence[dict[str, object]], options: ImportOptions
) -> TablePlan:
    model, key = TABLES[table]
    key_column = getattr(model, key)
    plan = TablePlan(table)
    keys = [str(r[key]) for r in rows]
    existing = {
        str(getattr(obj, key)): obj
        for obj in (await session.scalars(select(model).where(key_column.in_(keys)))).all()
    }
    total = await session.scalar(select(func.count()).select_from(model)) or 0
    plan.not_in_workbook = total - len(existing)

    for row in sorted(rows, key=lambda r: str(r[key])):
        current = existing.get(str(row[key]))
        if current is None:
            plan.inserts.append(row)
            continue
        changes = {col: v for col, v in row.items() if getattr(current, col) != v}
        if not changes:
            plan.unchanged += 1
        elif not options.allow_updates:
            plan.conflicts.append(Conflict(str(row[key]), sorted(changes)))
        elif any(col in PROTECTED_ONE_WAY and getattr(current, col) is True for col in changes):
            plan.rejected.append(Conflict(str(row[key]), sorted(changes)))
        else:
            plan.updates.append((str(row[key]), changes))
    return plan


async def _schema_ready(session: AsyncSession) -> bool:
    connection = await session.connection()
    return await connection.run_sync(lambda c: inspect(c).has_table("customer"))


async def build_plan(
    session: AsyncSession, data: ValidatedWorkbook, options: ImportOptions
) -> dict[str, TablePlan]:
    return {
        "customer": await _plan_table(
            session, "customer", [customer_columns(c) for c in data.customers], options
        ),
        "subscription": await _plan_table(
            session, "subscription", [subscription_columns(c) for c in data.customers], options
        ),
        "payment_history": await _plan_table(
            session, "payment_history", [history_columns(h) for h in data.history], options
        ),
        "customer_note": await _plan_table(
            session, "customer_note", [note_columns(n) for n in data.notes], options
        ),
        "call_scenario": await _plan_table(
            session, "call_scenario", [scenario_columns(s) for s in data.scenarios], options
        ),
    }


async def _apply(
    session: AsyncSession, data: ValidatedWorkbook, plan: dict[str, TablePlan], sha256: str
) -> None:
    for table, table_plan in plan.items():  # dict order: parents before children
        model, _ = TABLES[table]
        session.add_all(model(**row) for row in table_plan.inserts)
        for key, changes in table_plan.updates:
            obj = await session.get(model, key)
            for col, value in changes.items():
                setattr(obj, col, value)
            record_audit(
                session,
                event_type="import.record_updated",
                entity_type=table,
                entity_id=key,
                actor_type=ActorType.IMPORTER,
                details={"fields": sorted(changes), "workbook_sha256": sha256},
            )
        await session.flush()

    for c in data.customers:
        customer = await session.get(Customer, c.customer_id)
        if customer is not None:
            await refresh_customer_summary(session, customer)
            await refresh_notes_digest(session, customer)

    record_audit(
        session,
        event_type="import.completed",
        entity_type="workbook",
        entity_id=sha256[:16],
        actor_type=ActorType.IMPORTER,
        details={"workbook_sha256": sha256, "counts": {t: p.counts() for t, p in plan.items()}},
    )


async def run_import(
    session_factory: async_sessionmaker[AsyncSession], workbook_path: Path, options: ImportOptions
) -> ImportReport:
    raw = await asyncio.to_thread(read_workbook, workbook_path)  # openpyxl is synchronous
    validation, data = validate_workbook(raw)
    report = ImportReport("validation_failed", options, validation)
    if data is None:
        return report
    if validation.discrepancies and not options.accept_computed_summaries:
        report.outcome = "blocked"
        report.messages.append(
            "Workbook summaries disagree with values recomputed from canonical rows; review the "
            "discrepancies, then re-run with --accept-computed-summaries to store recomputed "
            "values."
        )
        return report

    async with session_factory() as session:
        ready = await _schema_ready(session)
        await session.rollback()  # close the inspection's implicit transaction
        if not ready:
            report.outcome = "failed"
            report.messages.append(
                "Database schema not initialised; run `uv run alembic upgrade head`."
            )
            return report
        try:
            # Leaving this block normally commits; any exception rolls everything back.
            async with session.begin():
                report.plan = await build_plan(session, data, options)
                conflicts = sum(len(p.conflicts) for p in report.plan.values())
                changes = sum(len(p.inserts) + len(p.updates) for p in report.plan.values())
                if conflicts:
                    report.outcome = "blocked"
                    report.messages.append(
                        f"{conflicts} existing record(s) differ from the workbook. Nothing was "
                        "written. Re-run with --allow-updates to overwrite the listed fields "
                        "(audit-logged)."
                    )
                elif options.dry_run:
                    report.outcome = "dry_run"
                elif not changes:
                    report.outcome = "no_changes"
                else:
                    await _apply(session, data, report.plan, raw.sha256)
                    report.outcome = "imported"
        except IntegrityError:
            logger.exception("import violated a database constraint")
            report.outcome = "failed"
            report.messages.append(
                "A database constraint rejected the import (e.g. a phone/email/subscription "
                "already "
                "used by another record). The transaction was rolled back; nothing was written."
            )
    return report
