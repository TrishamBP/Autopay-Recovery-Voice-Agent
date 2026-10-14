"""Conversation policy: (stage, validated intent, trusted facts) -> what happens next.

Pure and deterministic. The model contributes an intent and, for questions, candidate wording;
it never chooses a side effect. An action runs only when the validated intent maps to it in
`INTENT_ACTIONS` and the stage permits it (no payment action before identity is confirmed and
the customer agreed to continue). Spoken confirmations of actions are written here, after the
backend reports the real result, never by the model.
"""

from dataclasses import dataclass
from datetime import datetime

from src.common.enums import CallOutcome
from src.voice import prompts
from src.voice.prompts import CallFacts
from src.voice.schemas import INTENT_ACTIONS, AgentTurnOutput
from src.voice.vocabulary import ActionStatus, BackendAction, ResolutionPreference
from src.voice.vocabulary import ConversationIntent as I
from src.voice.vocabulary import ConversationStage as S

CALLING_WINDOW = "between 9 AM and 8 PM India time"
GOODBYE_NOT_THEM = "I'm sorry for the inconvenience. Thank you, and goodbye."
GOODBYE_LATER = "No problem. We'll try again another time. Thank you, goodbye."
GOODBYE_DECLINED = "I understand, and I respect your decision. Thank you for your time. Goodbye."
GOODBYE_UNCLEAR = (
    "I'm sorry, I'm having trouble understanding. We'll try again another time. Goodbye."
)
DNC_TEXT = "Understood. We won't call you again about this. Goodbye."
DNC_FAILED_TEXT = "Understood. I'll end the call now. Goodbye."
PAID_NOW = (
    "Our records now show this payment is complete, so there is nothing more you need "
    "to do. Thank you for your time. Goodbye."
)
UNCLEAR_INTENTS = frozenset({I.UNCLEAR_RESPONSE, I.OTHER})


@dataclass(frozen=True)
class Decision:
    intent: I
    next_stage: S
    text: str | None = None  # spoken when no action runs
    action: BackendAction | None = None
    end: bool = False
    outcome: CallOutcome | None = None
    preference: ResolutionPreference | None = None
    callback_time: str | None = None


def _clarify_or_end(intent: I, stage: S, streak: int, max_unclear: int, question: str) -> Decision:
    if streak >= max_unclear:
        return Decision(intent, S.ENDED, GOODBYE_UNCLEAR, end=True)
    return Decision(intent, stage, f"Sorry, I didn't quite catch that. {question}")


def decide(
    stage: S,
    intent: I,
    output: AgentTurnOutput | None,
    facts: CallFacts,
    *,
    unclear_streak: int,
    max_unclear: int,
) -> Decision:
    """`unclear_streak` = consecutive previous unclear/other turns in this call."""
    callback_time = output.callback_time if output else None
    preference = output.resolution_preference if output else None
    if intent == I.DO_NOT_CALL:  # always honoured, in every stage
        return Decision(
            intent,
            S.ENDED,
            action=BackendAction.RECORD_DO_NOT_CALL,
            end=True,
            outcome=CallOutcome.DO_NOT_CONTACT_REQUESTED,
        )
    if intent == I.IDENTITY_NOT_CONFIRMED:
        return Decision(
            intent, S.ENDED, GOODBYE_NOT_THEM, end=True, outcome=CallOutcome.WRONG_PERSON
        )

    if stage == S.IDENTITY_CHECK:
        if intent == I.IDENTITY_CONFIRMED:
            return Decision(intent, S.CONVENIENCE_CHECK)  # text = introduction (needs settings)
        if intent in (I.REQUEST_CALLBACK, I.DECLINE_ASSISTANCE):
            return Decision(intent, S.ENDED, GOODBYE_LATER, end=True)
        if intent == I.ASK_QUESTION:
            return Decision(intent, stage, prompts.who_is_calling(facts))
        return _clarify_or_end(
            intent, stage, unclear_streak, max_unclear, f"May I speak with {facts.customer_name}?"
        )

    if stage == S.CONVENIENCE_CHECK:
        if intent == I.AGREE_TO_CONTINUE:
            if facts.payment_state == "paid":
                return Decision(intent, S.ENDED, prompts.disclosure(facts), end=True)
            return Decision(
                intent,
                S.DISCUSSION,
                prompts.disclosure(facts),
                outcome=CallOutcome.FAILURE_EXPLAINED,
            )
        if intent == I.REQUEST_CALLBACK:
            if callback_time:
                return Decision(
                    intent,
                    S.CALLBACK_TIME,
                    action=BackendAction.SCHEDULE_CALLBACK,
                    preference=ResolutionPreference.CALLBACK,
                    callback_time=callback_time,
                )
            return Decision(
                intent,
                S.CALLBACK_TIME,
                "No problem. When would be a better time for us to call you back? "
                f"We can call {CALLING_WINDOW}.",
                preference=ResolutionPreference.CALLBACK,
            )
        if intent == I.DECLINE_ASSISTANCE:
            return Decision(
                intent, S.ENDED, GOODBYE_DECLINED, end=True, outcome=CallOutcome.CUSTOMER_DECLINED
            )
        if intent == I.ASK_QUESTION:
            return Decision(
                intent,
                stage,
                f"I'm calling about your subscription with {facts.company_name}. "
                "Is now a convenient time for a brief conversation?",
            )
        return _clarify_or_end(
            intent,
            stage,
            unclear_streak,
            max_unclear,
            "Is this a convenient time for a brief conversation?",
        )

    if stage == S.CALLBACK_TIME:
        if intent == I.REQUEST_CALLBACK and callback_time:
            return Decision(
                intent,
                stage,
                action=BackendAction.SCHEDULE_CALLBACK,
                preference=ResolutionPreference.CALLBACK,
                callback_time=callback_time,
            )
        return Decision(intent, S.ENDED, GOODBYE_LATER, end=True)

    # --- DISCUSSION ------------------------------------------------------------------------
    if facts.payment_state == "paid":
        return Decision(intent, S.ENDED, PAID_NOW, end=True)
    action = INTENT_ACTIONS.get(intent)
    if intent == I.REQUEST_CALLBACK and not callback_time:
        return Decision(
            intent,
            stage,
            f"Of course. What day and time would suit you? We can call {CALLING_WINDOW}.",
            preference=ResolutionPreference.CALLBACK,
        )
    if action is not None:
        return Decision(
            intent, stage, action=action, preference=preference, callback_time=callback_time
        )
    if intent == I.DECLINE_ASSISTANCE:
        return Decision(
            intent, S.ENDED, GOODBYE_DECLINED, end=True, outcome=CallOutcome.CUSTOMER_DECLINED
        )
    if intent in UNCLEAR_INTENTS and (output is None or intent == I.UNCLEAR_RESPONSE):
        return _clarify_or_end(
            intent, stage, unclear_streak, max_unclear, prompts.options_question(facts)
        )
    fallback = {
        I.CHECK_WITH_BANK: "That makes sense. Would you like us to call you back after you've "
        "spoken with your bank?",
        I.ASK_QUESTION: "I can explain the payment, arrange a secure payment link, or set up a "
        "callback. What would help most?",
    }.get(intent, prompts.options_question(facts))
    text = prompts.safe_say(output.spoken, fallback) if output else fallback
    end = bool(output and output.end_call)
    return Decision(
        intent,
        S.ENDED if end else stage,
        f"{text} Goodbye." if end else text,
        end=end,
        preference=preference,
    )


def action_text(
    action: BackendAction, status: ActionStatus, facts: CallFacts, detail: dict[str, object]
) -> tuple[str, bool, S | None, CallOutcome | None]:
    """(spoken text, end_call, stage override, outcome) for an action's VERIFIED result."""
    ok = status == ActionStatus.SUCCEEDED
    if action == BackendAction.RECORD_DO_NOT_CALL:
        return (
            (DNC_TEXT if ok else DNC_FAILED_TEXT),
            True,
            S.ENDED,
            (CallOutcome.DO_NOT_CONTACT_REQUESTED if ok else None),
        )
    if action == BackendAction.CHECK_RETRY_ELIGIBILITY:
        if facts.retry_available and facts.retry_scheduled_for:
            return (
                (
                    f"An automatic retry is already scheduled for {facts.retry_scheduled_for}. "
                    "I can't start a retry myself, but I can arrange a secure payment link if you "
                    "prefer. Would you like one?"
                ),
                False,
                None,
                None,
            )
        return (
            (
                "An automatic retry isn't available for this payment, and I can't start one "
                "myself. Would you like a secure payment link instead, or would you prefer to "
                "check with your bank first?"
            ),
            False,
            None,
            None,
        )
    if action == BackendAction.REQUEST_PAYMENT_LINK:
        if ok:
            return (
                (
                    "I've created a secure payment link for you. This is a test system, so it "
                    "won't take a real payment, and it hasn't been sent anywhere yet. Is there "
                    "anything else I can help with?"
                ),
                False,
                None,
                CallOutcome.PAYMENT_LINK_SENT,
            )
        return (
            (
                "I'm sorry, I couldn't create a payment link right now. Would you like a callback "
                "instead?"
            ),
            False,
            None,
            None,
        )
    if action == BackendAction.RECORD_PAYMENT_CLAIM:
        if ok:
            return (
                (
                    "Thank you for letting me know. I've asked our team to check the payment "
                    "status. Until it's confirmed, the payment still shows as unpaid on our side. "
                    "Is there anything else I can help with?"
                ),
                False,
                None,
                (CallOutcome.PAYMENT_CLAIM_LOGGED),
            )
        return (
            (
                "Thank you for letting me know. I wasn't able to record that just now, so our "
                "team may need to confirm it with you. Is there anything else?"
            ),
            False,
            None,
            None,
        )
    if action == BackendAction.RECORD_DISPUTE:
        if ok:
            return (
                (
                    "I understand. I've recorded your concern so our support team can review it, "
                    "and I won't discuss the payment further. Thank you, goodbye."
                ),
                True,
                (S.ENDED),
                CallOutcome.ESCALATED_TO_HUMAN,
            )
        return (
            (
                "I understand. I couldn't record your concern just now, so please contact our "
                "support team directly. Goodbye."
            ),
            True,
            S.ENDED,
            None,
        )
    if action == BackendAction.SCHEDULE_CALLBACK:
        if ok:
            when = _readable(str(detail.get("requested_at", "")))
            return (
                (f"Thank you. I've booked a callback for {when}. Goodbye."),
                True,
                S.ENDED,
                (CallOutcome.CALLBACK_SCHEDULED),
            )
        return (
            (
                "I'm sorry, I couldn't book that time. Callbacks are possible "
                f"{CALLING_WINDOW}, within the next two weeks. What time would suit you?"
            ),
            (False),
            S.CALLBACK_TIME,
            None,
        )
    if action == BackendAction.ESCALATE_TO_HUMAN:
        if ok:
            return (
                (
                    "I've recorded a request for our support team to contact you. I can't "
                    "transfer this call right now. Thank you, goodbye."
                ),
                True,
                S.ENDED,
                (CallOutcome.ESCALATED_TO_HUMAN),
            )
        return (
            (
                "I'm sorry, I couldn't record that request right now. Please contact our support "
                "team directly. Goodbye."
            ),
            True,
            S.ENDED,
            None,
        )
    raise ValueError(f"unhandled action {action}")  # pragma: no cover - enum is exhaustive


def _readable(iso: str) -> str:
    try:
        from zoneinfo import ZoneInfo

        local = datetime.fromisoformat(iso).astimezone(ZoneInfo("Asia/Kolkata"))
    except ValueError:
        return "the time you asked for"
    return f"{local.day} {local:%B} at {local:%I:%M %p}".replace(" 0", " ")
