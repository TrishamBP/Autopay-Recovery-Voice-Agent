"""Offline database operations. Run from the backend directory:

    uv run python -m scripts.manage validate-workbook [--workbook PATH]
    uv run python -m scripts.manage import [--dry-run] [--allow-updates]
                                           [--accept-computed-summaries]
    uv run python -m scripts.manage check          # integrity + derived-summary consistency
    uv run python -m scripts.manage counts         # rows per table
    uv run python -m scripts.manage purge-transcripts [--dry-run]   # retention policy
    uv run python -m scripts.manage sync-call-status CALL_SESSION_ID  # fetch status from Twilio

Schema migrations are separate: `uv run alembic upgrade head`.
Exit status: 0 success, 1 validation/integrity failure or blocked import, 2 usage error.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select, text

from src.calls.models import CallSession
from src.calls.transcripts import purge_expired_transcripts
from src.common.time import utc_now
from src.config import get_settings
from src.customer_notes.service import build_digest, list_notes
from src.customers.models import Customer
from src.database import create_engine, create_session_factory
from src.importers.excel_importer import ImportOptions, run_import
from src.importers.validation import validate_workbook
from src.importers.workbook import read_workbook
from src.models_registry import Base
from src.payment_history.service import compute_customer_summary, stored_summary
from src.telephony.status import apply_call_status


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


async def cmd_validate(workbook: Path) -> int:
    raw = await asyncio.to_thread(read_workbook, workbook)
    report, _ = validate_workbook(raw)
    _print(report.to_dict())
    return 0 if report.ok and not report.discrepancies else 1


async def cmd_import(workbook: Path, options: ImportOptions) -> int:
    engine = create_engine(get_settings())
    try:
        report = await run_import(create_session_factory(engine), workbook, options)
    finally:
        await engine.dispose()
    _print(report.to_dict())
    return 0 if report.outcome in ("imported", "no_changes", "dry_run") else 1


async def cmd_check() -> int:
    engine = create_engine(get_settings())
    problems: list[str] = []
    try:
        async with engine.connect() as conn:
            integrity = (await conn.execute(text("PRAGMA integrity_check"))).scalars().all()
            fk_violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            revision = (
                (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
                if "alembic_version" in tables
                else None
            )
        if integrity != ["ok"]:
            problems.append(f"integrity_check: {integrity}")
        if fk_violations:
            problems.append(f"foreign_key_check: {len(fk_violations)} violation(s)")
        missing = sorted(set(Base.metadata.tables) - tables)
        if missing:
            problems.append(f"missing tables (run migrations): {missing}")
        if not missing:
            async with create_session_factory(engine)() as session:
                for customer in (await session.scalars(select(Customer))).all():
                    if stored_summary(customer) != await compute_customer_summary(
                        session, customer.customer_id
                    ):
                        problems.append(
                            f"{customer.customer_id}: stored summary != payment_history"
                        )
                    digest = build_digest(await list_notes(session, customer.customer_id))
                    if (customer.customer_notes, customer.notes_updated_at) != digest:
                        problems.append(f"{customer.customer_id}: notes digest != customer_note")
    finally:
        await engine.dispose()
    _print({"ok": not problems, "alembic_revision": revision, "problems": problems})
    return 0 if not problems else 1


async def cmd_purge(dry_run: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        purged = await purge_expired_transcripts(
            create_session_factory(engine),
            now=utc_now(),
            retention_days=settings.transcript_retention_days,
            dry_run=dry_run,
        )
    finally:
        await engine.dispose()
    _print(
        {"dry_run": dry_run, "retention_days": settings.transcript_retention_days, "calls": purged}
    )
    return 0


async def cmd_sync_call_status(call_session_id: str) -> int:
    """Read-only fetch of a call's status from Twilio, applied through the idempotent status
    path (for trial accounts, which do not get status callbacks). Never creates a call."""
    settings = get_settings()
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        print("Twilio credentials are not configured", file=sys.stderr)
        return 2
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            call = await session.get(CallSession, call_session_id)
            sid = call.provider_call_id if call else None
        if not sid:
            print("call session not found or has no provider call id", file=sys.stderr)
            return 1
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value())
        remote = await asyncio.to_thread(lambda: client.calls(sid).fetch())
        duration = int(remote.duration) if remote.duration and remote.duration.isdigit() else None
        result = await apply_call_status(
            factory,
            call_session_id=call_session_id,
            provider_status=remote.status,
            provider_call_id=sid,
            sequence=None,
            duration_seconds=duration,
        )
    finally:
        await engine.dispose()
    _print(
        {
            "call_session_id": call_session_id,
            "twilio_status": remote.status,
            "duration": duration,
            "result": result,
        }
    )
    return 0


async def cmd_counts() -> int:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as conn:
            existing = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            counts = {}
            for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
                if table.name in existing:
                    counts[table.name] = (
                        await conn.execute(select(func.count()).select_from(table))
                    ).scalar_one()
    finally:
        await engine.dispose()
    _print(counts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.manage",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-workbook", "import"):
        p = sub.add_parser(name)
        p.add_argument("--workbook", type=Path, default=None)
        if name == "import":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--allow-updates", action="store_true")
            p.add_argument("--accept-computed-summaries", action="store_true")
    sub.add_parser("check")
    sub.add_parser("counts")
    sync = sub.add_parser("sync-call-status")
    sync.add_argument("call_session_id")
    purge = sub.add_parser("purge-transcripts")
    purge.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    workbook = getattr(args, "workbook", None) or get_settings().workbook_path
    if args.command in ("validate-workbook", "import") and not Path(workbook).is_file():
        print(f"workbook not found: {workbook}", file=sys.stderr)
        return 2
    if args.command == "validate-workbook":
        return asyncio.run(cmd_validate(workbook))
    if args.command == "import":
        options = ImportOptions(args.dry_run, args.allow_updates, args.accept_computed_summaries)
        return asyncio.run(cmd_import(workbook, options))
    if args.command == "check":
        return asyncio.run(cmd_check())
    if args.command == "sync-call-status":
        return asyncio.run(cmd_sync_call_status(args.call_session_id))
    if args.command == "purge-transcripts":
        return asyncio.run(cmd_purge(args.dry_run))
    return asyncio.run(cmd_counts())


if __name__ == "__main__":
    sys.exit(main())
