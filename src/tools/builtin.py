"""The initial tool set. Handlers are thin: they call domain services that enforce the rules."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.calls.service import schedule_callback
from src.common.enums import ActorType, NoteCreatedBy
from src.customer_notes.service import add_note
from src.customers.service import get_customer, mark_do_not_contact
from src.payment_history.service import compute_customer_summary
from src.payments.service import create_simulated_payment_link, payment_status_view
from src.tools.registry import ToolContext, ToolRegistry, ToolRuntime, ToolSpec
from src.tools.schemas import (
    AddCustomerNoteArgs,
    NoArgs,
    RequestPaymentLinkArgs,
    ScheduleCallbackArgs,
    ToolArgs,
)

_NOTE_AUTHOR = {ActorType.VOICE_AGENT: NoteCreatedBy.VOICE_AGENT}


async def _get_payment_status(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    return payment_status_view(await get_customer(session, ctx.customer_id))


async def _get_payment_history_summary(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    await get_customer(session, ctx.customer_id)
    summary = await compute_customer_summary(session, ctx.customer_id)  # canonical rows
    return {
        "summary": summary.payment_history_summary,
        "payments_on_time_6m": summary.payments_on_time_6m,
        "payments_late_6m": summary.payments_late_6m,
        "payments_failed_6m": summary.payments_failed_6m,
        "payments_missed_6m": summary.payments_missed_6m,
        "minimum_amount_paid_6m": (
            None
            if summary.minimum_amount_paid_6m is None
            else f"{summary.minimum_amount_paid_6m:.2f}"
        ),
        "total_amount_paid_6m": f"{summary.total_amount_paid_6m:.2f}",
        "payment_reliability_score": summary.payment_reliability_score,
        "score_note": "operational indicator only, not a credit score",
    }


async def _add_customer_note(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    assert isinstance(args, AddCustomerNoteArgs)  # noqa: S101  guaranteed by the registry
    note = await add_note(
        session,
        customer_id=ctx.customer_id,
        note_type=args.note_type,
        note_text=args.note_text,
        created_by=_NOTE_AUTHOR.get(ctx.actor_type, NoteCreatedBy.HUMAN_AGENT),
        actor_type=ctx.actor_type,
        source_call_id=ctx.call_session_id,
        clock=rt.clock,
    )
    return {"note_id": note.note_id, "note_type": note.note_type.value}


async def _schedule_callback(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    assert isinstance(args, ScheduleCallbackArgs)  # noqa: S101
    callback = await schedule_callback(
        session,
        customer_id=ctx.customer_id,
        requested_at=args.requested_at,
        timezone=args.timezone,
        settings=rt.settings,
        actor_type=ctx.actor_type,
        call_session_id=ctx.call_session_id,
        clock=rt.clock,
    )
    return {
        "callback_id": callback.id,
        "requested_at": callback.requested_at.isoformat(),
        "timezone": callback.timezone,
    }


async def _mark_do_not_contact(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    changed = await mark_do_not_contact(
        session,
        ctx.customer_id,
        actor_type=ctx.actor_type,
        call_session_id=ctx.call_session_id,
        clock=rt.clock,
    )
    return {"do_not_contact": True, "changed": changed}


async def _request_payment_link(
    session: AsyncSession, ctx: ToolContext, args: ToolArgs, rt: ToolRuntime
) -> dict[str, object]:
    assert isinstance(args, RequestPaymentLinkArgs)  # noqa: S101
    link = await create_simulated_payment_link(
        session,
        customer_id=ctx.customer_id,
        purpose=args.purpose,
        settings=rt.settings,
        actor_type=ctx.actor_type,
        call_session_id=ctx.call_session_id,
        clock=rt.clock,
    )
    # No token/URL: the model must never see or speak it.
    return {
        "link_id": link.id,
        "purpose": link.purpose.value,
        "status": link.status.value,
        "simulated": True,
        "delivered": False,
        "expires_at": link.expires_at.isoformat(),
    }


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in (
        ToolSpec(
            "get_payment_status",
            "Current failed charge facts for the bound customer.",
            NoArgs,
            _get_payment_status,
            side_effecting=False,
        ),
        ToolSpec(
            "get_payment_history_summary",
            "Six-cycle payment summary computed from payment history.",
            NoArgs,
            _get_payment_history_summary,
            side_effecting=False,
        ),
        ToolSpec(
            "add_customer_note",
            "Store a short operational note (no payment credentials).",
            AddCustomerNoteArgs,
            _add_customer_note,
            side_effecting=True,
        ),
        ToolSpec(
            "schedule_callback",
            "Book a callback inside permitted calling hours.",
            ScheduleCallbackArgs,
            _schedule_callback,
            side_effecting=True,
        ),
        ToolSpec(
            "mark_do_not_contact",
            "Record the customer's request to stop calls (one-way).",
            NoArgs,
            _mark_do_not_contact,
            side_effecting=True,
        ),
        ToolSpec(
            "request_payment_link",
            "Create a SIMULATED payment link (never charges; URL never returned).",
            RequestPaymentLinkArgs,
            _request_payment_link,
            side_effecting=True,
        ),
    ):
        registry.register(spec)
    return registry
