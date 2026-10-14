import dataclasses

import pytest
from pydantic import ValidationError

from src.common.enums import CallOutcome
from src.common.redaction import redact_transcript, strip_secrets
from src.config import Settings
from src.voice import policy, prompts
from src.voice.schemas import AgentTurnOutput
from src.voice.twiml import gather_response, hangup_response, hold_response, reject_response
from src.voice.vocabulary import ActionStatus, BackendAction
from src.voice.vocabulary import ConversationIntent as I
from src.voice.vocabulary import ConversationStage as S

SETTINGS = Settings(_env_file=None, public_base_url="https://demo.example.invalid")
FACTS = prompts.CallFacts(
    customer_name="Asha Rao",
    company_name="Acme Streaming",
    agent_name="Alex",
    subscription_name="Premium Monthly",
    amount="799 rupees",
    due_date="4 October 2026",
    payment_state="failed",
    failure_reason_code="insufficient_funds",
    retry_available=False,
    retry_scheduled_for=None,
    link_available=True,
)


def out(intent: str, spoken: str = "Okay.", **kw: object) -> AgentTurnOutput:
    return AgentTurnOutput.model_validate({"spoken_response": spoken, "intent": intent} | kw)


# --- TwiML ---------------------------------------------------------------------------------


def test_gather_uses_absolute_url_and_documented_speech_settings() -> None:
    xml = gather_response(SETTINGS, "Hello <there> & welcome", call_session_id="abc", turn=3)
    assert (
        'action="https://demo.example.invalid/api/v1/twilio/process-speech?'
        'call_session_id=abc&amp;turn=3"'
    ) in xml
    for attr in (
        'input="speech"',
        'language="en-IN"',
        'speechModel="experimental_conversations"',
        'speechTimeout="2"',
        'actionOnEmptyResult="true"',
        'voice="Polly.Raveena"',
    ):
        assert attr in xml
    assert 'speechTimeout="auto"' not in xml  # not allowed together with speechModel
    assert "&lt;there&gt; &amp; welcome" in xml


def test_hangup_hold_and_reject() -> None:
    assert "<Hangup" in hangup_response(SETTINGS, "Bye")
    assert "<Say" not in reject_response()
    hold = hold_response(SETTINGS, call_session_id="abc", turn=2)
    assert "<Pause" in hold and "turn=2</Redirect>" in hold


# --- Scripts and disclosure order -------------------------------------------------------------


def test_opening_scripts_disclose_nothing_before_agreement() -> None:
    opening = prompts.opening(FACTS)
    intro = prompts.introduction(FACTS, SETTINGS.transcript_disclosure)
    assert opening == "Hello, may I speak with Asha Rao?"
    assert "my name is Alex" in intro and "automated payment assistance assistant" in intro
    assert "Acme Streaming" in intro and "transcript" in intro
    for text in (opening, intro):
        assert "799" not in text and "October" not in text and "insufficient" not in text
    disclosure = prompts.disclosure(FACTS)
    assert (
        "Thank you. I'm calling regarding your Premium Monthly subscription payment of "
        "799 rupees. The automatic payment scheduled for 4 October 2026 was unsuccessful "
        "because the bank reported insufficient funds."
    ) in disclosure
    assert disclosure.endswith(
        "Would you prefer to receive a secure payment link, or check with your bank first?"
    )


@pytest.mark.parametrize(
    ("changes", "expected", "absent"),
    [
        (
            {"failure_reason_code": "mandate_inactive"},
            "automatic payment authorization with your bank could not be used",
            "insufficient",
        ),
        ({"failure_reason_code": None}, "payment status is being checked", "unsuccessful"),
        ({"payment_state": "pending_reconciliation"}, "still being confirmed", "unsuccessful"),
        ({"payment_state": "paid"}, "nothing you need to do", "unsuccessful"),
        (
            {"retry_available": True, "retry_scheduled_for": "8 October"},
            "wait for the automatic retry, receive a secure payment link, or check",
            "zzz",
        ),
        ({"link_available": False}, "Would you prefer to check with your bank first?", "link"),
    ],
)
def test_disclosure_describes_only_the_verified_state(
    changes: dict, expected: str, absent: str
) -> None:
    text = prompts.disclosure(dataclasses.replace(FACTS, **changes))
    assert expected in text and absent not in text


# --- Rule-based classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "utterance", "intent"),
    [
        (S.IDENTITY_CHECK, "Yes, speaking", I.IDENTITY_CONFIRMED),
        (S.IDENTITY_CHECK, "that's me", I.IDENTITY_CONFIRMED),
        (S.IDENTITY_CHECK, "Sorry, wrong number", I.IDENTITY_NOT_CONFIRMED),
        (S.IDENTITY_CHECK, "No, she's not home", I.IDENTITY_NOT_CONFIRMED),
        (S.IDENTITY_CHECK, "Please stop calling me", I.DO_NOT_CALL),
        (S.CONVENIENCE_CHECK, "Sure, go ahead", I.AGREE_TO_CONTINUE),
        (S.CONVENIENCE_CHECK, "I'm busy right now", I.REQUEST_CALLBACK),
        (S.CONVENIENCE_CHECK, "Not interested, thanks", I.DECLINE_ASSISTANCE),
        (S.DISCUSSION, "don't call me again", I.DO_NOT_CALL),
        (S.IDENTITY_CHECK, "Who is this?", None),
        (S.DISCUSSION, "Yes please send a link", None),  # discussion is model-classified
    ],
)
def test_rule_classification(stage: S, utterance: str, intent: I | None) -> None:
    assert prompts.classify_by_rules(stage, utterance) == intent


# --- Validated schema -------------------------------------------------------------------------


def test_schema_rejects_unknown_intents_and_actions() -> None:
    with pytest.raises(ValidationError):
        out("PAY_NOW")
    with pytest.raises(ValidationError):
        out("ASK_QUESTION", backend_action="charge_card")
    with pytest.raises(ValidationError):
        out("REQUEST_CALLBACK", callback_time="tomorrow 6pm")
    with pytest.raises(ValidationError):
        AgentTurnOutput.model_validate({"intent": "ASK_QUESTION"})
    ok = out("REQUEST_CALLBACK", callback_time="2026-10-15T18:00", extra_field="ignored")
    assert ok.callback_time == "2026-10-15T18:00"


# --- Policy -----------------------------------------------------------------------------------


def decide(
    stage: S,
    intent: I,
    output: AgentTurnOutput | None = None,
    facts: prompts.CallFacts = FACTS,
    streak: int = 0,
) -> policy.Decision:
    return policy.decide(stage, intent, output, facts, unclear_streak=streak, max_unclear=1)


@pytest.mark.parametrize(
    "stage", [S.IDENTITY_CHECK, S.CONVENIENCE_CHECK, S.DISCUSSION, S.CALLBACK_TIME]
)
def test_do_not_call_is_honoured_in_every_stage(stage: S) -> None:
    d = decide(stage, I.DO_NOT_CALL)
    assert d.action == BackendAction.RECORD_DO_NOT_CALL and d.end


def test_no_payment_action_before_identity_and_agreement() -> None:
    for stage in (S.IDENTITY_CHECK, S.CONVENIENCE_CHECK):
        for intent in (I.REQUEST_PAYMENT_LINK, I.RETRY_PAYMENT, I.ALREADY_PAID, I.DISPUTE_PAYMENT):
            d = decide(stage, intent, out(intent.value, backend_action="request_payment_link"))
            assert d.action is None, (stage, intent)
            assert d.text is None or "799" not in d.text


def test_identity_stage_transitions() -> None:
    assert decide(S.IDENTITY_CHECK, I.IDENTITY_CONFIRMED).next_stage == S.CONVENIENCE_CHECK
    wrong = decide(S.IDENTITY_CHECK, I.IDENTITY_NOT_CONFIRMED)
    assert wrong.end and wrong.outcome == CallOutcome.WRONG_PERSON
    assert "Alex" in (decide(S.IDENTITY_CHECK, I.ASK_QUESTION).text or "")
    first = decide(S.IDENTITY_CHECK, I.UNCLEAR_RESPONSE)
    assert not first.end and "May I speak with Asha Rao?" in (first.text or "")
    assert decide(S.IDENTITY_CHECK, I.UNCLEAR_RESPONSE, streak=1).end


def test_convenience_stage_transitions() -> None:
    agree = decide(S.CONVENIENCE_CHECK, I.AGREE_TO_CONTINUE)
    assert agree.next_stage == S.DISCUSSION and "799 rupees" in (agree.text or "")
    busy = decide(S.CONVENIENCE_CHECK, I.REQUEST_CALLBACK)
    assert busy.next_stage == S.CALLBACK_TIME and busy.action is None
    declined = decide(S.CONVENIENCE_CHECK, I.DECLINE_ASSISTANCE)
    assert declined.end and declined.outcome == CallOutcome.CUSTOMER_DECLINED
    paid = decide(
        S.CONVENIENCE_CHECK,
        I.AGREE_TO_CONTINUE,
        facts=dataclasses.replace(FACTS, payment_state="paid"),
    )
    assert paid.end and "complete" in (paid.text or "")


@pytest.mark.parametrize(
    ("intent", "action"),
    [
        (I.RETRY_PAYMENT, BackendAction.CHECK_RETRY_ELIGIBILITY),
        (I.REQUEST_PAYMENT_LINK, BackendAction.REQUEST_PAYMENT_LINK),
        (I.ALREADY_PAID, BackendAction.RECORD_PAYMENT_CLAIM),
        (I.DISPUTE_PAYMENT, BackendAction.RECORD_DISPUTE),
        (I.REQUEST_HUMAN_SUPPORT, BackendAction.ESCALATE_TO_HUMAN),
    ],
)
def test_discussion_actions_follow_the_intent_not_the_model_field(
    intent: I, action: BackendAction
) -> None:
    d = decide(S.DISCUSSION, intent, out(intent.value, backend_action="record_do_not_call"))
    assert d.action == action  # the model's mismatched backend_action is ignored


@pytest.mark.parametrize("intent", [I.ASK_QUESTION, I.CHECK_WITH_BANK, I.OTHER])
def test_conversational_intents_never_run_actions(intent: I) -> None:
    d = decide(
        S.DISCUSSION,
        intent,
        out(
            intent.value,
            "Happy to help.",
            requires_backend_action=True,
            backend_action="request_payment_link",
        ),
    )
    assert d.action is None and d.text == "Happy to help."


def test_callback_needs_a_time_and_unclear_ends_after_one_clarification() -> None:
    ask = decide(S.DISCUSSION, I.REQUEST_CALLBACK, out("REQUEST_CALLBACK"))
    assert ask.action is None and "What day and time" in (ask.text or "")
    book = decide(
        S.DISCUSSION, I.REQUEST_CALLBACK, out("REQUEST_CALLBACK", callback_time="2026-10-15T18:00")
    )
    assert book.action == BackendAction.SCHEDULE_CALLBACK
    assert not decide(S.DISCUSSION, I.UNCLEAR_RESPONSE, out("UNCLEAR_RESPONSE")).end
    assert decide(S.DISCUSSION, I.UNCLEAR_RESPONSE, out("UNCLEAR_RESPONSE"), streak=1).end
    assert decide(S.DISCUSSION, I.DECLINE_ASSISTANCE, out("DECLINE_ASSISTANCE")).end


def test_model_text_is_guarded() -> None:
    d = decide(S.DISCUSSION, I.ASK_QUESTION, out("ASK_QUESTION", "Your payment was received!"))
    assert "received" not in (d.text or "")
    d = decide(S.DISCUSSION, I.ASK_QUESTION, out("ASK_QUESTION", "Please tell me your OTP"))
    assert "OTP" not in (d.text or "")


def test_action_texts_only_claim_verified_results() -> None:
    ok, _, _, outcome = policy.action_text(
        BackendAction.REQUEST_PAYMENT_LINK, ActionStatus.SUCCEEDED, FACTS, {}
    )
    failed, *_ = policy.action_text(
        BackendAction.REQUEST_PAYMENT_LINK, ActionStatus.FAILED, FACTS, {}
    )
    assert "created a secure payment link" in ok and outcome == CallOutcome.PAYMENT_LINK_SENT
    assert "hasn't been sent" in ok and "couldn't create" in failed
    claim, *_ = policy.action_text(
        BackendAction.RECORD_PAYMENT_CLAIM, ActionStatus.SUCCEEDED, FACTS, {}
    )
    assert "still shows as unpaid" in claim
    booked, end, *_ = policy.action_text(
        BackendAction.SCHEDULE_CALLBACK,
        ActionStatus.SUCCEEDED,
        FACTS,
        {"requested_at": "2026-10-15T12:30:00+00:00"},
    )
    assert "15 October at 6:00 PM" in booked and end
    rejected, end, stage, _ = policy.action_text(
        BackendAction.SCHEDULE_CALLBACK, ActionStatus.REJECTED, FACTS, {}
    )
    assert "couldn't book" in rejected and not end and stage == S.CALLBACK_TIME
    retry, *_ = policy.action_text(
        BackendAction.CHECK_RETRY_ELIGIBILITY, ActionStatus.NOT_AVAILABLE, FACTS, {}
    )
    assert "can't start one myself" in retry


# --- Redaction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "redacted"),
    [
        ("my OTP is 4 8 2 1 9 3", "my OTP is [REDACTED_SECRET]"),
        ("card 4111 1111 1111 1111 exp", "card [REDACTED_CARD] exp"),
        ("call me on +91 98765 43210 now", "call me on [REDACTED_PHONE] now"),
        ("account 123456789 ok", "account [REDACTED_NUMBER] ok"),
        ("write to a.b@example.com", "write to [REDACTED_EMAIL]"),
        ("the cvv: 123", "the cvv: [REDACTED_SECRET]"),
        ("I paid 799 rupees on 4 October 2026", "I paid 799 rupees on 4 October 2026"),
    ],
)
def test_transcript_redaction(raw: str, redacted: str) -> None:
    assert redact_transcript(raw) == redacted


def test_strip_secrets_keeps_ordinary_text() -> None:
    assert strip_secrets("password is hunter2") == "password is [REDACTED_SECRET]"
    assert strip_secrets("I will pay on Friday") == "I will pay on Friday"
