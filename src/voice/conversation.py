"""Customer conversation engine for Twilio speech webhooks (turn-based <Gather>/<Say>).

Per call: IDENTITY_CHECK -> CONVENIENCE_CHECK -> DISCUSSION (or CALLBACK_TIME) -> ENDED.
Opening lines are fixed scripts filled from trusted records; the model (Fireworks/GLM) only
classifies intent and drafts wording for questions, and its JSON is validated. `policy.decide`
chooses any action; `ActionExecutor` runs it through the existing typed tools.

Trust: the webhook signature proves Twilio sent the request; `call_session_id` comes from the
signed URL we generated; the request's CallSid (and AccountSid in live mode) must match the
session, which must belong to an approved recovery call. Nothing from the model or the request
body selects a customer.

Persistence (one turn N = customer sequence 2N-1 + agent sequence 2N, greeting = 0):
1. the customer turn is stored (status `received`) BEFORE the model is called;
2. the model and any action run with no transaction open;
3. the agent turn, the customer turn's intent/action/result and call events commit together
   BEFORE TwiML is returned. Duplicate deliveries replay the stored reply; a duplicate that
   arrives while the first is still processing gets a short hold + redirect.
"""

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.billing.models import BillingInstallment, PaymentAttempt
from src.billing.state_machine import InstallmentStatus
from src.calls.events import record_call_event
from src.calls.models import CallSession, ConversationTurn
from src.common.enums import ActorType, CallOutcome, CallStatus, PaymentStatus, TurnRole
from src.common.money import paise_to_rupees
from src.common.redaction import redact_transcript, strip_secrets
from src.common.time import Clock, utc_now
from src.config import Settings
from src.customers.models import Customer
from src.llm.client import ChatMessage, LLMClient, LLMError
from src.llm.parsing import parse_json_object
from src.recovery import service as recovery
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import RecoveryCaseStatus as R
from src.recovery.state_machine import RecoveryOutcome
from src.simulation.events import EventStatus, EventType
from src.simulation.models import SimulationEvent, SimulationState
from src.tools.registry import ToolRegistry
from src.voice import policy, prompts, twiml
from src.voice.actions import ActionExecutor, ActionResult
from src.voice.schemas import AgentTurnOutput
from src.voice.vocabulary import (
    ActionStatus,
    BackendAction,
    CallEventType,
    DeliveryStatus,
    EventSource,
    ResolutionPreference,
    TranscriptConsent,
    TurnStatus,
)
from src.voice.vocabulary import ConversationIntent as Intent
from src.voice.vocabulary import ConversationStage as Stage

logger = logging.getLogger(__name__)

CONVERSABLE_CASES = frozenset({R.CALL_INITIATED, R.CALL_INITIATION_UNKNOWN})
ACTIVE_CALL = frozenset({CallStatus.QUEUED, CallStatus.IN_PROGRESS})
FALLBACK = "Sorry, I'm having a little trouble right now. Could you say that again?"
TECHNICAL_GOODBYE = (
    "Sorry, we are having technical difficulties. We will follow up another time. Goodbye."
)
SILENCE_PROMPT = "Sorry, I didn't catch that. Could you please repeat?"
SILENCE_GOODBYE = "I'm sorry, I can't hear you. We will try again another time. Goodbye."
TURN_LIMIT_GOODBYE = "Thank you for your time. We will follow up if needed. Goodbye."
OPTED_OUT_GOODBYE = "We won't take any more of your time. Goodbye."
NOT_NEEDED = "Hello, this is an automated assistant. Sorry to have disturbed you. Goodbye."
WITHHELD_PRE_NOTICE = "[not retained: before transcript notice]"
WITHHELD_POLICY = "[not retained]"
_ACTION_EVENTS = {
    BackendAction.RECORD_PAYMENT_CLAIM: CallEventType.CUSTOMER_REPORTED_ALREADY_PAID,
    BackendAction.SCHEDULE_CALLBACK: CallEventType.CUSTOMER_REQUESTED_CALLBACK,
    BackendAction.RECORD_DISPUTE: CallEventType.CUSTOMER_DISPUTED_PAYMENT,
    BackendAction.RECORD_DO_NOT_CALL: CallEventType.CUSTOMER_REQUESTED_NO_FURTHER_CALLS,
}


class CallRejected(Exception):
    """Verification failed; the caller gets a bare hangup and no account details."""


class PaymentResolved(Exception):
    """Trusted records now show the installment paid: end politely, no persuasion."""


PAYMENT_OUTCOMES = frozenset(
    {
        RecoveryOutcome.PAYMENT_SUCCEEDED,
        RecoveryOutcome.ALREADY_PAID,
        RecoveryOutcome.PAYMENT_RECEIVED_AFTER_CONTACT,
    }
)


@dataclass(frozen=True)
class TwilioParams:
    call_sid: str | None
    account_sid: str | None
    speech: str
    confidence: float | None

    @classmethod
    def from_form(cls, form: dict[str, str]) -> "TwilioParams":
        try:
            confidence = float(form["Confidence"]) if form.get("Confidence") else None
        except ValueError:
            confidence = None
        return cls(
            form.get("CallSid") or None,
            form.get("AccountSid") or None,
            (form.get("SpeechResult") or "").strip()[:1000],
            confidence,
        )


@dataclass(frozen=True)
class TurnSnapshot:
    """Plain copy of a stored turn (ORM rows expire when their transaction ends)."""

    role: TurnRole
    text: str
    intent: Intent | None
    llm: str | None
    silence: bool


@dataclass(frozen=True)
class Reply:
    text: str
    end: bool
    next_stage: Stage
    intent: Intent | None = None
    preference: ResolutionPreference | None = None
    action: ActionResult | None = None
    source: EventSource = EventSource.SYSTEM
    status: TurnStatus = TurnStatus.PROCESSED
    error_category: str | None = None
    outcome: CallOutcome | None = None


@dataclass(frozen=True)
class TurnContext:
    call_session_id: str
    customer_id: str
    case_id: str
    turn: int
    stage: Stage
    facts: prompts.CallFacts
    history: list[TurnSnapshot]
    started: datetime


def _trailing_silence(history: list[TurnSnapshot]) -> Iterator[TurnSnapshot]:
    for t in reversed(history):
        if t.role != TurnRole.CUSTOMER:
            continue
        if not t.silence:
            return
        yield t


def _unclear_streak(history: list[TurnSnapshot]) -> int:
    streak = 0
    for t in reversed(history):
        if t.role != TurnRole.CUSTOMER:
            continue
        if t.intent not in policy.UNCLEAR_INTENTS:
            break
        streak += 1
    return streak


class VoiceConversationService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        tools: ToolRegistry,
        settings: Settings,
        clock: Clock = utc_now,
    ) -> None:
        self.factory, self.llm, self.settings, self.clock = factory, llm, settings, clock
        self.actions = ActionExecutor(factory, tools, settings, clock)
        # Turns still being decided after the webhook got a hold reply (owned; see aclose).
        self._turn_tasks: set[asyncio.Task[str]] = set()

    def _turn_done(self, task: asyncio.Task[str]) -> None:
        self._turn_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "deferred voice turn failed", extra={"error": type(task.exception()).__name__}
            )

    async def aclose(self) -> None:
        """Cancel turns still deciding at shutdown (the stale turn is re-run on redirect)."""
        for task in self._turn_tasks:
            task.cancel()
        if self._turn_tasks:
            async with asyncio.timeout(5):
                await asyncio.gather(*self._turn_tasks, return_exceptions=True)

    # --- verification & facts -------------------------------------------------------------------

    async def _verify(
        self,
        session: AsyncSession,
        call_session_id: str,
        params: TwilioParams,
        *,
        replay: bool = False,
    ) -> tuple[CallSession, RecoveryCase, Customer]:
        call = await session.get(CallSession, call_session_id)
        if call is None or call.recovery_case_id is None:
            raise CallRejected("unknown call session")
        if params.call_sid is None:
            raise CallRejected("missing CallSid")
        if call.provider_call_id is not None and params.call_sid != call.provider_call_id:
            raise CallRejected("CallSid does not match the session")
        if call.provider == "twilio" and params.account_sid != self.settings.twilio_account_sid:
            raise CallRejected("AccountSid does not match")
        case = await session.get(RecoveryCase, call.recovery_case_id)
        customer = await session.get(Customer, call.customer_id)
        if replay and case is not None and customer is not None:
            # Re-serving a stored reply changes nothing; it must still work after that reply
            # ended the conversation (e.g. the redirect after a deferred goodbye).
            return call, case, customer
        if case is not None and case.recovery_outcome in PAYMENT_OUTCOMES:
            raise PaymentResolved
        if case is None or customer is None or case.status not in CONVERSABLE_CASES:
            raise CallRejected("recovery case is not in an active call state")
        if call.status not in ACTIVE_CALL or call.conversation_stage == Stage.ENDED:
            raise CallRejected("call session is not active")
        return call, case, customer

    async def _facts(
        self, session: AsyncSession, case: RecoveryCase, customer: Customer
    ) -> prompts.CallFacts:
        installment = await session.get(BillingInstallment, case.installment_id)
        sim = await session.get(SimulationState, case.simulation_id)
        if installment is None or sim is None:
            raise CallRejected("recovery case data is incomplete")
        tz = ZoneInfo(sim.timezone)
        reason = await session.scalar(
            select(PaymentAttempt.failure_reason)
            .where(
                PaymentAttempt.installment_id == installment.installment_id,
                PaymentAttempt.failure_reason.is_not(None),
            )
            .order_by(PaymentAttempt.attempt_number.desc())
            .limit(1)
        )
        retry_at = await session.scalar(
            select(SimulationEvent.scheduled_at_virtual).where(
                SimulationEvent.simulation_id == sim.simulation_id,
                SimulationEvent.event_type == EventType.RETRY_DUE,
                SimulationEvent.aggregate_id == installment.installment_id,
                SimulationEvent.status == EventStatus.PENDING,
            )
        )
        state = {
            InstallmentStatus.SUCCESS: "paid",
            InstallmentStatus.FAILED: "failed",
            InstallmentStatus.RETRY_SCHEDULED: "retry_scheduled",
        }.get(installment.payment_status, "pending_reconciliation")
        rupees = paise_to_rupees(installment.amount_minor_units)
        amount = f"{rupees:,.0f}" if rupees == rupees.to_integral() else f"{rupees:,.2f}"
        due = installment.due_at.astimezone(tz)
        retry_local = retry_at.astimezone(tz) if retry_at else None
        return prompts.CallFacts(
            customer_name=customer.full_name,
            company_name=self.settings.voice_business_name,
            agent_name=self.settings.voice_agent_name,
            subscription_name=customer.subscription_plan,
            amount=f"{amount} rupees",
            due_date=f"{due.day} {due:%B %Y}",
            payment_state=state,
            failure_reason_code=reason.value if reason else None,
            retry_available=installment.payment_status == InstallmentStatus.RETRY_SCHEDULED
            and retry_local is not None,
            retry_scheduled_for=f"{retry_local.day} {retry_local:%B}" if retry_local else None,
            link_available=not customer.do_not_contact
            and customer.payment_status in (PaymentStatus.FAILED, PaymentStatus.RETRY_SCHEDULED),
        )

    def _customer_text(self, stage: Stage, speech: str) -> tuple[str, str | None]:
        """(sanitized text, original text) per the transcript policy."""
        if not speech:
            return "", None
        if self.settings.transcript_policy == "metadata_only":
            return WITHHELD_POLICY, None
        if stage == Stage.IDENTITY_CHECK:  # the transcript notice comes with the introduction
            return WITHHELD_PRE_NOTICE, None
        original = strip_secrets(speech) if self.settings.transcript_store_original else None
        return redact_transcript(speech), original

    # --- /twilio/voice -------------------------------------------------------------------------

    async def start(self, call_session_id: str, params: TwilioParams) -> str:
        """The callee answered: ask for the intended customer (no details yet)."""
        try:
            async with self.factory() as session, session.begin():
                call, case, customer = await self._verify(session, call_session_id, params)
                if customer.do_not_contact:
                    return twiml.hangup_response(self.settings, OPTED_OUT_GOODBYE)
                facts = await self._facts(session, case, customer)
                if call.provider_call_id is None:  # an answered call proves an "unknown" start
                    call.provider_call_id = params.call_sid
                    if call.provider == "twilio":
                        call.twilio_call_sid = params.call_sid
                if case.status == R.CALL_INITIATION_UNKNOWN:
                    recovery.transition(
                        session,
                        case,
                        R.CALL_INITIATED,
                        actor_type=ActorType.SYSTEM,
                        reason="reconciled_by_voice_webhook",
                    )
                call.status = CallStatus.IN_PROGRESS
                call.started_at = call.started_at or self.clock()
                if self.settings.transcript_policy == "metadata_only":
                    call.transcript_consent = TranscriptConsent.NOT_RETAINED
                await record_call_event(
                    session,
                    call,
                    CallEventType.CALL_ANSWERED,
                    source=EventSource.TWILIO,
                    key="status:CALL_ANSWERED",
                )
                if facts.payment_state == "paid":  # no recovery conversation for a paid item
                    call.conversation_stage = Stage.ENDED
                    await record_call_event(
                        session,
                        call,
                        CallEventType.CONVERSATION_ENDED,
                        source=EventSource.BACKEND,
                        key="conversation:ended",
                        details={"reason": "installment_paid"},
                    )
                    return twiml.hangup_response(self.settings, NOT_NEEDED)
                call.conversation_stage = call.conversation_stage or Stage.IDENTITY_CHECK
                existing = await session.scalar(
                    select(ConversationTurn).where(
                        ConversationTurn.call_session_id == call.id, ConversationTurn.sequence == 0
                    )
                )
                text = existing.transcript_text if existing else prompts.opening(facts)
                if existing is None:
                    now = self.clock()
                    session.add(
                        ConversationTurn(
                            call_session_id=call.id,
                            sequence=0,
                            turn_number=0,
                            role=TurnRole.AGENT,
                            transcript_text=text,
                            provider="script",
                            idempotency_key=f"{call.id}:turn:0:agent",
                            customer_id=call.customer_id,
                            recovery_case_id=case.recovery_case_id,
                            received_at=now,
                            processed_at=now,
                            processing_status=TurnStatus.PROCESSED,
                            delivery_status=DeliveryStatus.RETURNED,
                        )
                    )
                    await record_call_event(
                        session,
                        call,
                        CallEventType.CONVERSATION_STARTED,
                        source=EventSource.BACKEND,
                        key="conversation:started",
                    )
                    await record_call_event(
                        session,
                        call,
                        CallEventType.AGENT_RESPONSE_GENERATED,
                        source=EventSource.SYSTEM,
                        key="turn:0:agent",
                        turn_number=0,
                    )
        except PaymentResolved:
            return twiml.hangup_response(self.settings, NOT_NEEDED)
        except CallRejected as exc:
            logger.warning("voice webhook rejected", extra={"reason": str(exc)})
            return twiml.reject_response()
        return twiml.gather_response(self.settings, text, call_session_id=call_session_id, turn=1)

    # --- /twilio/process-speech ------------------------------------------------------------------

    async def handle_speech(self, call_session_id: str, turn: int, params: TwilioParams) -> str:
        try:
            ctx_or_xml = await self._receive(call_session_id, turn, params)
        except PaymentResolved:
            return twiml.hangup_response(self.settings, policy.PAID_NOW)
        except CallRejected as exc:
            logger.warning("speech webhook rejected", extra={"reason": str(exc)})
            return twiml.reject_response()
        if isinstance(ctx_or_xml, str):
            return ctx_or_xml
        # Decide in an owned task: if it outlasts the webhook budget, Twilio gets a short
        # hold + redirect for the same turn, which _receive answers with the stored reply.
        task = asyncio.create_task(self._complete(ctx_or_xml, params))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_done)
        try:
            async with asyncio.timeout(self.settings.voice_webhook_reply_within_seconds):
                return await asyncio.shield(task)
        except TimeoutError:
            logger.info(
                "voice turn deferred", extra={"call_session_id": call_session_id, "turn": turn}
            )
            return twiml.hold_response(self.settings, call_session_id=call_session_id, turn=turn)

    async def _complete(self, ctx: TurnContext, params: TwilioParams) -> str:
        call_session_id, turn = ctx.call_session_id, ctx.turn
        reply = await self._decide(ctx, params)
        try:
            await self._finish(ctx, reply)
        except IntegrityError:  # a concurrent duplicate finished first: replay its reply
            stored = await self._stored_agent(call_session_id, turn)
            if stored is not None:
                return self._render(stored[0], stored[1], call_session_id, turn)
            return await self._persist_failed(ctx, "agent_turn_conflict")
        except SQLAlchemyError:
            logger.exception(
                "voice turn persistence failed",
                extra={"call_session_id": call_session_id, "turn": turn},
            )
            return await self._persist_failed(ctx, "persistence_error")
        logger.info(
            "voice turn",
            extra={
                "call_session_id": call_session_id,
                "turn": turn,
                "stage": ctx.stage.value,
                "intent": reply.intent.value if reply.intent else None,
                "status": reply.status.value,
                "action": reply.action.action.value if reply.action else None,
                "ended": reply.end,
                "speech_chars": len(params.speech),
                "speech_confidence": params.confidence,
            },
        )
        return self._render(reply.text, reply.end, call_session_id, turn)

    async def _stored_agent(self, call_session_id: str, turn: int) -> tuple[str, bool] | None:
        async with self.factory() as session:
            agent = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.call_session_id == call_session_id,
                    ConversationTurn.sequence == 2 * turn,
                )
            )
            if agent is None:
                return None
            return agent.transcript_text, bool((agent.provider_metadata or {}).get("ended"))

    async def _receive(
        self, call_session_id: str, turn: int, params: TwilioParams
    ) -> TurnContext | str:
        """Verify, deduplicate and store the incoming customer turn (own transaction)."""
        stale_after = timedelta(seconds=self.settings.voice_llm_timeout_seconds + 10)
        for _ in range(2):  # the second pass only happens after losing an insert race
            stored = await self._stored_agent(call_session_id, turn)
            async with self.factory() as session:
                call, case, customer = await self._verify(
                    session, call_session_id, params, replay=stored is not None
                )
                if stored is not None:  # Twilio retried this webhook: replay, change nothing
                    return self._render(stored[0], stored[1], call_session_id, turn)
                existing = await session.scalar(
                    select(ConversationTurn).where(
                        ConversationTurn.call_session_id == call.id,
                        ConversationTurn.sequence == 2 * turn - 1,
                    )
                )
                if existing is not None and existing.processing_status == TurnStatus.RECEIVED:
                    received = existing.received_at
                    if received is not None and self.clock() - received < stale_after:
                        return twiml.hold_response(
                            self.settings, call_session_id=call_session_id, turn=turn
                        )
                if customer.do_not_contact:
                    return twiml.hangup_response(self.settings, OPTED_OUT_GOODBYE)
                customer_turns = (
                    await session.scalar(
                        select(func.count())
                        .select_from(ConversationTurn)
                        .where(
                            ConversationTurn.call_session_id == call.id,
                            ConversationTurn.role == TurnRole.CUSTOMER,
                        )
                    )
                    or 0
                )
                if existing is None and turn != customer_turns + 1:
                    raise CallRejected("unexpected turn number")
                stage = call.conversation_stage or Stage.IDENTITY_CHECK
                facts = await self._facts(session, case, customer)
                rows = (
                    await session.scalars(
                        select(ConversationTurn)
                        .where(
                            ConversationTurn.call_session_id == call.id,
                            ConversationTurn.sequence < 2 * turn - 1,
                        )
                        .order_by(ConversationTurn.sequence)
                    )
                ).all()
                history = [
                    TurnSnapshot(
                        r.role,
                        r.transcript_text,
                        r.intent,
                        (r.provider_metadata or {}).get("llm"),
                        bool((r.provider_metadata or {}).get("silence")),
                    )
                    for r in rows
                ]
                ctx = TurnContext(
                    call.id,
                    customer.customer_id,
                    case.recovery_case_id,
                    turn,
                    stage,
                    facts,
                    history,
                    call.started_at or self.clock(),
                )
                sim = await session.get(SimulationState, case.simulation_id)
                virtual = sim.current_virtual_time if sim else None
                await session.rollback()
                if existing is not None:  # an earlier attempt died mid-turn: finish it now
                    return ctx
                text, original = self._customer_text(stage, params.speech)
                now = self.clock()
                try:
                    async with session.begin():
                        call = await session.get(CallSession, call_session_id)
                        session.add(
                            ConversationTurn(
                                call_session_id=call_session_id,
                                sequence=2 * turn - 1,
                                turn_number=turn,
                                role=TurnRole.CUSTOMER,
                                transcript_text=text,
                                original_text=original,
                                provider="twilio-gather",
                                model=self.settings.twilio_speech_model,
                                idempotency_key=f"{call_session_id}:turn:{turn}:customer",
                                customer_id=ctx.customer_id,
                                recovery_case_id=ctx.case_id,
                                received_at=now,
                                virtual_time=virtual,
                                processing_status=TurnStatus.RECEIVED,
                                provider_metadata={
                                    "confidence": params.confidence,
                                    "silence": not params.speech,
                                },
                            )
                        )
                        await session.flush()
                        previous = await session.scalar(
                            select(ConversationTurn).where(
                                ConversationTurn.call_session_id == call_session_id,
                                ConversationTurn.sequence == 2 * turn - 2,
                            )
                        )
                        if previous is not None:  # Twilio ran the prompt that led here
                            previous.delivery_status = DeliveryStatus.PLAYED
                        if call is not None:
                            await record_call_event(
                                session,
                                call,
                                CallEventType.CONVERSATION_TURN_RECEIVED,
                                source=EventSource.TWILIO,
                                key=f"turn:{turn}:received",
                                turn_number=turn,
                                virtual_time=virtual,
                                details={
                                    "silence": not params.speech,
                                    "confidence": params.confidence,
                                    "stage": stage.value,
                                },
                            )
                except IntegrityError:
                    continue  # a concurrent delivery inserted it first: re-check
                return ctx
        return twiml.hold_response(self.settings, call_session_id=call_session_id, turn=turn)

    # --- decision ------------------------------------------------------------------------------

    async def _model(self, ctx: TurnContext, speech: str) -> AgentTurnOutput:
        s = self.settings
        today = self.clock().astimezone(ZoneInfo(s.customer_default_timezone)).date().isoformat()
        messages = [ChatMessage("system", prompts.system_prompt(ctx.facts, ctx.stage, today))]
        recent = ctx.history[-s.voice_history_turns :] if s.voice_history_turns else []
        for t in recent:
            if t.text and not t.text.startswith("[not retained"):
                messages.append(
                    ChatMessage("assistant" if t.role == TurnRole.AGENT else "user", t.text)
                )
        messages.append(ChatMessage("user", speech))
        async with asyncio.timeout(s.voice_llm_timeout_seconds):  # whole budget, incl. re-ask
            for attempt in range(2):
                content = await self.llm.complete(
                    messages, max_tokens=s.voice_llm_max_tokens, temperature=0.2
                )
                try:
                    return AgentTurnOutput.model_validate(parse_json_object(content))
                except (ValidationError, LLMError) as exc:
                    if attempt == 1:
                        raise ValueError("model output failed validation") from exc
                    messages.append(
                        ChatMessage(
                            "user",
                            "Your previous reply was not valid. Reply with ONLY the JSON "
                            "object in the required format.",
                        )
                    )
        raise ValueError("unreachable")  # pragma: no cover

    async def _decide(self, ctx: TurnContext, params: TwilioParams) -> Reply:
        s = self.settings
        speech = params.speech
        if not speech:
            silent = 1 + sum(1 for _ in _trailing_silence(ctx.history))
            if silent > s.voice_max_silence_prompts:
                return Reply(SILENCE_GOODBYE, end=True, next_stage=Stage.ENDED)
            return Reply(SILENCE_PROMPT, end=False, next_stage=ctx.stage)
        if ctx.turn > s.voice_max_turns or (
            self.clock() - ctx.started > timedelta(seconds=s.voice_max_call_seconds)
        ):
            return Reply(TURN_LIMIT_GOODBYE, end=True, next_stage=Stage.ENDED)

        output: AgentTurnOutput | None = None
        source = EventSource.SYSTEM
        intent = prompts.classify_by_rules(ctx.stage, speech)
        if intent is None:
            try:
                output = await self._model(ctx, speech)
                intent, source = output.intent, EventSource.LLM
            except (TimeoutError, LLMError, ValueError) as exc:
                logger.warning("voice llm unavailable", extra={"error": type(exc).__name__})
                fallbacks = sum(
                    1 for t in ctx.history[-4:] if t.role == TurnRole.AGENT and t.llm == "fallback"
                )
                category = (
                    "llm_timeout"
                    if isinstance(exc, TimeoutError)
                    else "llm_invalid_output"
                    if isinstance(exc, ValueError)
                    else "llm_error"
                )
                if fallbacks >= 1:
                    return Reply(
                        TECHNICAL_GOODBYE,
                        end=True,
                        next_stage=Stage.ENDED,
                        status=TurnStatus.FALLBACK,
                        error_category=category,
                    )
                return Reply(
                    FALLBACK,
                    end=False,
                    next_stage=ctx.stage,
                    status=TurnStatus.FALLBACK,
                    error_category=category,
                )

        decision = policy.decide(
            ctx.stage,
            intent,
            output,
            ctx.facts,
            unclear_streak=_unclear_streak(ctx.history),
            max_unclear=s.voice_max_unclear,
        )
        text = decision.text
        if decision.next_stage == Stage.CONVENIENCE_CHECK and text is None:
            text = prompts.introduction(ctx.facts, s.transcript_disclosure)
        if decision.action is None:
            return Reply(
                text or FALLBACK,
                end=decision.end,
                next_stage=decision.next_stage,
                intent=intent,
                preference=decision.preference,
                source=source,
                outcome=decision.outcome,
            )
        result = await self.actions.run(
            decision.action,
            facts=ctx.facts,
            call_session_id=ctx.call_session_id,
            customer_id=ctx.customer_id,
            turn=ctx.turn,
            callback_time=decision.callback_time,
        )
        spoken, end, stage, outcome = policy.action_text(
            decision.action, result.status, ctx.facts, result.detail
        )
        return Reply(
            spoken,
            end=end,
            next_stage=stage or decision.next_stage,
            intent=intent,
            preference=decision.preference,
            action=result,
            source=source,
            outcome=outcome,
        )

    # --- persistence ---------------------------------------------------------------------------

    async def _finish(self, ctx: TurnContext, reply: Reply) -> None:
        """Agent turn + customer-turn results + events in ONE transaction, before replying."""
        now = self.clock()
        async with self.factory() as session, session.begin():
            call = await session.get(CallSession, ctx.call_session_id)
            customer_turn = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.call_session_id == ctx.call_session_id,
                    ConversationTurn.sequence == 2 * ctx.turn - 1,
                )
            )
            if call is None or customer_turn is None:
                raise SQLAlchemyError("turn context disappeared")
            action = reply.action
            customer_turn.intent = reply.intent
            customer_turn.resolution_preference = reply.preference
            customer_turn.backend_action = action.action.value if action else None
            customer_turn.action_status = action.status.value if action else None
            customer_turn.action_result = action.detail if action else None
            customer_turn.processing_status = reply.status
            customer_turn.error_category = reply.error_category
            customer_turn.processed_at = now
            llm_tag = (
                "fallback"
                if reply.status == TurnStatus.FALLBACK
                else "ok"
                if reply.source == EventSource.LLM
                else "skipped"
            )
            session.add(
                ConversationTurn(
                    call_session_id=ctx.call_session_id,
                    sequence=2 * ctx.turn,
                    turn_number=ctx.turn,
                    role=TurnRole.AGENT,
                    transcript_text=reply.text,
                    provider="fireworks" if reply.source == EventSource.LLM else "script",
                    model=self.settings.fireworks_model
                    if reply.source == EventSource.LLM
                    else None,
                    idempotency_key=f"{ctx.call_session_id}:turn:{ctx.turn}:agent",
                    customer_id=ctx.customer_id,
                    recovery_case_id=ctx.case_id,
                    received_at=now,
                    processed_at=now,
                    processing_status=reply.status,
                    delivery_status=DeliveryStatus.RETURNED,
                    provider_metadata={"ended": reply.end, "llm": llm_tag},
                )
            )
            if (
                ctx.stage == Stage.IDENTITY_CHECK
                and reply.next_stage == Stage.CONVENIENCE_CHECK
                and self.settings.transcript_policy == "store_sanitized"
            ):
                call.transcript_consent = TranscriptConsent.DISCLOSED
            call.conversation_stage = reply.next_stage
            if reply.outcome is not None:
                call.recovery_outcome = reply.outcome
            key = f"turn:{ctx.turn}"
            if reply.status == TurnStatus.FALLBACK:
                await record_call_event(
                    session,
                    call,
                    CallEventType.LLM_FALLBACK_USED,
                    source=EventSource.BACKEND,
                    key=f"{key}:fallback",
                    turn_number=ctx.turn,
                    details={"error_category": reply.error_category},
                )
            await record_call_event(
                session,
                call,
                CallEventType.AGENT_RESPONSE_GENERATED,
                source=reply.source,
                key=f"{key}:agent",
                turn_number=ctx.turn,
                details={
                    "intent": reply.intent.value if reply.intent else None,
                    "stage": reply.next_stage.value,
                },
            )
            if action is not None:
                await self._action_events(session, call, ctx, action)
            if reply.end:
                await record_call_event(
                    session,
                    call,
                    CallEventType.CONVERSATION_ENDED,
                    source=EventSource.BACKEND,
                    key="conversation:ended",
                    turn_number=ctx.turn,
                    details={"outcome": reply.outcome.value if reply.outcome else None},
                )

    async def _action_events(
        self, session: AsyncSession, call: CallSession, ctx: TurnContext, action: ActionResult
    ) -> None:
        key = f"turn:{ctx.turn}"
        ok = action.status == ActionStatus.SUCCEEDED
        await record_call_event(
            session,
            call,
            CallEventType.BACKEND_ACTION_REQUESTED,
            source=EventSource.BACKEND,
            key=f"{key}:action-requested",
            turn_number=ctx.turn,
            details={"action": action.action.value},
        )
        await record_call_event(
            session,
            call,
            CallEventType.BACKEND_ACTION_SUCCEEDED if ok else CallEventType.BACKEND_ACTION_FAILED,
            source=EventSource.BACKEND,
            key=f"{key}:action-result",
            turn_number=ctx.turn,
            details={
                "action": action.action.value,
                "status": action.status.value,
                "error_category": action.detail.get("error_category"),
            },
        )
        customer_event = _ACTION_EVENTS.get(action.action)
        if customer_event is not None:
            await record_call_event(
                session,
                call,
                customer_event,
                source=EventSource.BACKEND,
                key=f"{key}:customer",
                turn_number=ctx.turn,
                details={"action_status": action.status.value},
            )
        if action.action == BackendAction.RECORD_DO_NOT_CALL and ok:
            case = await session.get(RecoveryCase, ctx.case_id)
            sim = await session.get(SimulationState, case.simulation_id) if case else None
            if case is not None and case.status in CONVERSABLE_CASES and sim is not None:
                recovery.opt_out(
                    session,
                    case,
                    virtual_time=sim.current_virtual_time,
                    actor_type=ActorType.VOICE_AGENT,
                )

    async def _persist_failed(self, ctx: TurnContext, category: str) -> str:
        """The reply could not be stored: end safely and record the failure if possible. The
        goodbye is NOT recorded as a stored agent turn, so the transcript never claims it."""
        try:
            async with self.factory() as session, session.begin():
                customer_turn = await session.scalar(
                    select(ConversationTurn).where(
                        ConversationTurn.call_session_id == ctx.call_session_id,
                        ConversationTurn.sequence == 2 * ctx.turn - 1,
                    )
                )
                if customer_turn is not None and (
                    customer_turn.processing_status != TurnStatus.PROCESSED
                ):
                    customer_turn.processing_status = TurnStatus.FAILED
                    customer_turn.error_category = category
        except SQLAlchemyError:
            logger.exception(
                "could not record the turn failure", extra={"call_session_id": ctx.call_session_id}
            )
        return twiml.hangup_response(self.settings, TECHNICAL_GOODBYE)

    def _render(self, text: str, end: bool, call_session_id: str, turn: int) -> str:
        if end:
            return twiml.hangup_response(self.settings, text)
        return twiml.gather_response(
            self.settings, text, call_session_id=call_session_id, turn=turn + 1
        )
