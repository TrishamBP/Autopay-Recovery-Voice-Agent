"""Executes policy-authorised actions through the EXISTING typed tools (idempotent per call,
turn and tool). Returns the verified result; nothing here trusts the model.

Real vs simulated:
- payment link: simulated link record (no money, no delivery channel yet)
- callback, do-not-call, notes: real records in this database
- "already paid" / dispute / human support: recorded as customer notes + call events for the
  team; there is no live reconciliation or human queue integration yet
- retry: read-only eligibility check; retries are driven by the simulation clock only
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.common.enums import ActorType, NoteType
from src.common.time import Clock
from src.config import Settings
from src.tools.registry import ToolContext, ToolRegistry, ToolRuntime
from src.tools.service import execute_tool
from src.voice.prompts import CallFacts
from src.voice.vocabulary import ActionStatus, BackendAction

_NOTES = {
    BackendAction.RECORD_PAYMENT_CLAIM: (
        NoteType.PAYMENT_CONTEXT,
        "Customer stated during a recovery call that the payment was already made. Status check "
        "requested; payment status unchanged until confirmed.",
    ),
    BackendAction.RECORD_DISPUTE: (
        NoteType.SUPPORT_CONTEXT,
        "Customer disputed the payment during a recovery call. Recovery persuasion stopped; "
        "needs support review.",
    ),
    BackendAction.ESCALATE_TO_HUMAN: (
        NoteType.SUPPORT_CONTEXT,
        "Customer asked to speak with a human during a recovery call. Support follow-up needed.",
    ),
}
_TOOL_STATUS = {
    "succeeded": ActionStatus.SUCCEEDED,
    "rejected": ActionStatus.REJECTED,
    "failed": ActionStatus.FAILED,
}


@dataclass(frozen=True)
class ActionResult:
    action: BackendAction
    status: ActionStatus
    detail: dict[str, object] = field(default_factory=dict)  # redacted tool result


class ActionExecutor:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tools: ToolRegistry,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self.factory, self.tools, self.settings, self.clock = factory, tools, settings, clock

    async def _tool(
        self,
        name: str,
        args: dict[str, object],
        *,
        call_session_id: str,
        customer_id: str,
        turn: int,
    ) -> tuple[ActionStatus, dict[str, object]]:
        async with self.factory() as session:
            result = await execute_tool(
                session,
                self.tools,
                ToolRuntime(self.settings, clock=self.clock),
                name=name,
                raw_args=args,
                context=ToolContext(customer_id, ActorType.VOICE_AGENT, call_session_id),
                idempotency_key=f"voice:{call_session_id}:{turn}:{name}",
            )
        detail = dict(result.data)
        if result.error_category:
            detail["error_category"] = result.error_category
        return _TOOL_STATUS[result.status], detail

    async def run(
        self,
        action: BackendAction,
        *,
        facts: CallFacts,
        call_session_id: str,
        customer_id: str,
        turn: int,
        callback_time: str | None,
    ) -> ActionResult:
        ids = {"call_session_id": call_session_id, "customer_id": customer_id, "turn": turn}
        if action == BackendAction.CHECK_RETRY_ELIGIBILITY:
            status = ActionStatus.SUCCEEDED if facts.retry_available else ActionStatus.NOT_AVAILABLE
            return ActionResult(action, status, {"retry_scheduled_for": facts.retry_scheduled_for})
        if action == BackendAction.REQUEST_PAYMENT_LINK:
            if not facts.link_available:
                return ActionResult(action, ActionStatus.NOT_AVAILABLE)
            status, detail = await self._tool("request_payment_link", {"purpose": "payment"}, **ids)
            return ActionResult(
                action,
                status,
                {
                    k: v
                    for k, v in detail.items()
                    if k in ("link_id", "simulated", "delivered", "error_category")
                },
            )
        if action == BackendAction.SCHEDULE_CALLBACK:
            if not callback_time:
                return ActionResult(action, ActionStatus.REJECTED, {"error_category": "no_time"})
            status, detail = await self._tool(
                "schedule_callback",
                {
                    "requested_at": callback_time,
                    "timezone": self.settings.customer_default_timezone,
                },
                **ids,
            )
            return ActionResult(action, status, detail)
        if action == BackendAction.RECORD_DO_NOT_CALL:
            status, detail = await self._tool("mark_do_not_contact", {}, **ids)
            return ActionResult(action, status, detail)
        note_type, text = _NOTES[action]
        status, detail = await self._tool(
            "add_customer_note", {"note_type": note_type.value, "note_text": text}, **ids
        )
        return ActionResult(
            action, status, {k: v for k, v in detail.items() if k in ("note_id", "error_category")}
        )
