from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.models import AuditLog
from src.calls.models import CallbackRequest
from src.common.enums import ActorType, RecoveryStatus, ToolExecutionStatus
from src.config import Settings
from src.customer_notes.models import CustomerNote
from src.customers.models import Customer
from src.payments.models import PaymentLink
from src.tools.builtin import build_default_registry
from src.tools.models import ToolExecution
from src.tools.registry import ToolContext, ToolRuntime
from src.tools.schemas import ToolResult
from src.tools.service import execute_tool

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 10, 14, 4, 30, tzinfo=UTC)  # 10:00 IST, a Wednesday
REGISTRY = build_default_registry()


async def run(
    factory: Factory,
    settings: Settings,
    name: str,
    args: dict,
    *,
    customer: str = "CUS-1001",
    key: str | None = "k1",
    actor: ActorType = ActorType.VOICE_AGENT,
) -> ToolResult:
    async with factory() as session:
        return await execute_tool(
            session,
            REGISTRY,
            ToolRuntime(settings, clock=lambda: NOW),
            name=name,
            raw_args=args,
            context=ToolContext(customer, actor),
            idempotency_key=key,
        )


async def count(factory: Factory, model: type) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0


pytestmark = pytest.mark.usefixtures("seeded")


async def test_add_note_creates_note_digest_audit_and_execution(
    session_factory: Factory, settings: Settings
) -> None:
    result = await run(
        session_factory,
        settings,
        "add_customer_note",
        {
            "note_type": "customer_preference",
            "note_text": "Customer prefers weekday evening calls.",
        },
    )
    assert result.status == "succeeded", result
    assert result.data == {"note_id": "NOTE-0004", "note_type": "customer_preference"}
    async with session_factory() as s:
        note = await s.get(CustomerNote, "NOTE-0004")
        customer = await s.get(Customer, "CUS-1001")
        assert note is not None and note.created_by.value == "voice_agent"
        assert customer is not None
        assert customer.customer_notes.endswith("Customer prefers weekday evening calls.")
        assert customer.notes_updated_at == NOW
        audit = (await s.scalars(select(AuditLog.event_type))).all()
        assert "customer_note.created" in audit
        execution = await s.scalar(select(ToolExecution))
        assert execution is not None and execution.status == ToolExecutionStatus.SUCCEEDED


async def test_unknown_customer_and_unknown_tool(
    session_factory: Factory, settings: Settings
) -> None:
    missing = await run(session_factory, settings, "get_payment_status", {}, customer="CUS-9999")
    assert (missing.status, missing.error_category) == ("rejected", "customer_not_found")
    unknown = await run(session_factory, settings, "drop_tables", {})
    assert (unknown.status, unknown.error_category) == ("rejected", "unknown_tool")


@pytest.mark.parametrize(
    "args",
    [
        {"note_type": "gossip", "note_text": "hello"},
        {"note_type": "callback", "note_text": ""},
        {"note_type": "callback", "note_text": "x" * 501},
        {"note_type": "callback", "note_text": "ok", "customer_id": "CUS-1002"},  # LLM-supplied ID
    ],
)
async def test_invalid_note_arguments_are_rejected(
    session_factory: Factory, settings: Settings, args: dict
) -> None:
    result = await run(session_factory, settings, "add_customer_note", args)
    assert (result.status, result.error_category) == ("rejected", "invalid_arguments")
    assert await count(session_factory, CustomerNote) == 3


async def test_secret_like_note_is_rejected_and_recorded(
    session_factory: Factory, settings: Settings
) -> None:
    result = await run(
        session_factory,
        settings,
        "add_customer_note",
        {"note_type": "callback", "note_text": "Customer said OTP is 4321"},
    )
    assert result.status == "rejected" and result.error_category == "invalid_arguments"
    assert await count(session_factory, CustomerNote) == 3
    async with session_factory() as s:
        execution = await s.scalar(select(ToolExecution))
        assert execution is not None and execution.status == ToolExecutionStatus.REJECTED


async def test_idempotency_replays_and_detects_conflicts(
    session_factory: Factory, settings: Settings
) -> None:
    args = {"note_type": "callback", "note_text": "Call after 6 PM."}
    first = await run(session_factory, settings, "add_customer_note", args, key="turn-7")
    second = await run(session_factory, settings, "add_customer_note", args, key="turn-7")
    assert first.status == second.status == "succeeded"
    assert second.replayed and second.data == first.data
    assert await count(session_factory, CustomerNote) == 4  # exactly one new note

    conflict = await run(
        session_factory,
        settings,
        "add_customer_note",
        {**args, "note_text": "Different"},
        key="turn-7",
    )
    assert conflict.error_category == "idempotency_conflict"
    missing = await run(session_factory, settings, "add_customer_note", args, key=None)
    assert missing.error_category == "missing_idempotency_key"


async def test_do_not_contact_is_enforced_for_later_contact_actions(
    session_factory: Factory, settings: Settings
) -> None:
    dnc = await run(session_factory, settings, "mark_do_not_contact", {}, key="dnc")
    assert dnc.status == "succeeded" and dnc.data["changed"] is True
    callback = await run(
        session_factory,
        settings,
        "schedule_callback",
        {"requested_at": "2026-10-15T18:00:00", "timezone": "Asia/Kolkata"},
        key="cb",
    )
    link = await run(session_factory, settings, "request_payment_link", {}, key="link")
    assert callback.error_category == link.error_category == "business_rule_violation"
    assert await count(session_factory, CallbackRequest) == 0
    assert await count(session_factory, PaymentLink) == 0
    async with session_factory() as s:
        customer = await s.get(Customer, "CUS-1001")
        assert customer is not None
        assert customer.do_not_contact and customer.recovery_status == RecoveryStatus.OPTED_OUT
    again = await run(session_factory, settings, "mark_do_not_contact", {}, key="dnc-2")
    assert again.data["changed"] is False


@pytest.mark.parametrize(
    ("args", "category"),
    [
        ({"requested_at": "2026-10-15T18:00:00", "timezone": "Mars/Olympus"}, "invalid_arguments"),
        (
            {"requested_at": "2026-10-13T18:00:00", "timezone": "Asia/Kolkata"},
            "business_rule_violation",
        ),
        (
            {"requested_at": "2026-10-15T23:00:00", "timezone": "Asia/Kolkata"},
            "business_rule_violation",
        ),
        (
            {"requested_at": "2026-11-30T12:00:00", "timezone": "Asia/Kolkata"},
            "business_rule_violation",
        ),
        ({"requested_at": "not a time"}, "invalid_arguments"),
    ],
)
async def test_callback_validation(
    session_factory: Factory, settings: Settings, args: dict, category: str
) -> None:
    result = await run(session_factory, settings, "schedule_callback", args)
    assert (result.status, result.error_category) == ("rejected", category)
    assert await count(session_factory, CallbackRequest) == 0


async def test_callback_in_other_timezone_is_converted(
    session_factory: Factory, settings: Settings
) -> None:
    # 08:00 in London (BST, UTC+1) == 12:30 IST: inside calling hours.
    result = await run(
        session_factory,
        settings,
        "schedule_callback",
        {"requested_at": "2026-10-15T08:00:00", "timezone": "Europe/London"},
    )
    assert result.status == "succeeded", result
    async with session_factory() as s:
        callback = await s.scalar(select(CallbackRequest))
        customer = await s.get(Customer, "CUS-1001")
        assert callback is not None
        assert callback.requested_at == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
        assert customer is not None
        assert customer.recovery_status == RecoveryStatus.CALLBACK_SCHEDULED
        assert customer.payment_status.value == "failed"  # a callback is not a recovery


async def test_payment_link_is_simulated_and_never_exposes_token(
    session_factory: Factory, settings: Settings
) -> None:
    result = await run(session_factory, settings, "request_payment_link", {"purpose": "payment"})
    assert result.status == "succeeded"
    assert result.data["simulated"] is True and result.data["delivered"] is False
    async with session_factory() as s:
        link = await s.scalar(select(PaymentLink))
        customer = await s.get(Customer, "CUS-1001")
        assert link is not None and customer is not None
        assert link.token not in str(result.model_dump())
        assert customer.payment_status.value == "failed"
        assert customer.payment_link_url is not None
        assert customer.payment_link_url.startswith("https://payments.example.invalid/")


async def test_read_tools_return_db_facts(session_factory: Factory, settings: Settings) -> None:
    status = await run(session_factory, settings, "get_payment_status", {}, customer="CUS-1002")
    assert status.data["amount"] == "499.00"
    assert status.data["payment_failure_reason"] == "expired_card"
    assert "payment_link_url" not in status.data
    summary = await run(
        session_factory, settings, "get_payment_history_summary", {}, customer="CUS-1002"
    )
    assert summary.data["payment_reliability_score"] == 50
    assert summary.data["total_amount_paid_6m"] == "1707.00"


def test_registry_exposes_typed_definitions() -> None:
    names = REGISTRY.names()
    assert names == sorted(
        [
            "get_payment_status",
            "get_payment_history_summary",
            "add_customer_note",
            "schedule_callback",
            "mark_do_not_contact",
            "request_payment_link",
        ]
    )
    definitions = REGISTRY.llm_tool_definitions()
    note = next(d for d in definitions if d["function"]["name"] == "add_customer_note")
    assert "customer_id" not in str(note["function"]["parameters"])
