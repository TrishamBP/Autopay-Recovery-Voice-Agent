"""Conversation engine end to end: signed Twilio webhooks -> staged opening -> scripted GLM ->
policy -> existing typed tools. No network, no credentials, no real calls."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.billing.models import BillingInstallment, PaymentAttempt
from src.calls.models import CallbackRequest, CallSession, ConversationTurn
from src.customer_notes.models import CustomerNote
from src.customers.models import Customer
from src.payments.models import PaymentLink
from src.telephony.mock import MockTelephonyProvider
from src.voice import conversation as conv
from src.voice import policy
from tests.conftest import FakeLLM
from tests.integration.test_twilio_voice import (
    APPROVAL,
    SIM,
    answer,
    app_client,
    post_signed,
    say,
    speech_path,
    voice_settings,
)
from tests.sim_fixtures import seed_customers, three_customers

Factory = async_sessionmaker[AsyncSession]


def model(intent: str, spoken: str = "Okay.", **kw: object) -> str:
    return "<think>reasoning</think>" + json.dumps(
        {"spoken_response": spoken, "intent": intent} | kw
    )


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def api(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory, llm: FakeLLM
) -> AsyncIterator[httpx.AsyncClient]:
    await seed_customers(session_factory, three_customers())
    async for http in app_client(voice_settings(tmp_path), llm, MockTelephonyProvider()):
        yield http


async def approved(
    api: httpx.AsyncClient,
    scenario: str = "MANDATE_INACTIVE",
    target: str = "2026-10-10T12:00:00+05:30",
) -> tuple[dict, dict]:
    await api.post(
        "/api/v1/simulation",
        json={
            "simulation_id": SIM,
            "scenario_assignments": [
                {"subscription_id": "SUB-3001", "scenario_id": "SUCCESSFUL_PAYMENT"},
                {"subscription_id": "SUB-3002", "scenario_id": scenario},
                {"subscription_id": "SUB-3003", "scenario_id": "SUCCESSFUL_PAYMENT"},
            ],
        },
    )
    await api.post("/api/v1/simulation/advance", json={"simulation_id": SIM, "target_time": target})
    case = (await api.get("/api/v1/recovery/queue", params={"simulation_id": SIM})).json()["items"][
        0
    ]
    call = (
        await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    ).json()
    return case, call


async def to_discussion(api: httpx.AsyncClient, call: dict) -> str:
    await answer(api, call)
    await say(api, call, 1, "Yes, speaking")
    return (await say(api, call, 2, "Sure, go ahead")).text


async def count(factory: Factory, model_cls: type, *where: object) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model_cls).where(*where)) or 0


# --- Opening and disclosure order ------------------------------------------------------------


async def test_opening_discloses_details_only_after_identity_and_agreement(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    _, call = await approved(api)
    opening = (await answer(api, call)).text
    intro = (await say(api, call, 1, "Yes, speaking")).text
    disclosure = (await say(api, call, 2, "Sure, go ahead")).text
    assert "Hello, may I speak with Test Customer 2?" in opening
    assert "Hi Test Customer 2, my name is Alex" in intro
    assert "automated payment assistance assistant" in intro and "transcript" in intro
    for text in (opening, intro):
        assert "799" not in text and "October" not in text
    assert (
        "Test Plan subscription payment of 799 rupees. The automatic payment scheduled for "
        "4 October 2026 was unsuccessful because the automatic payment authorization with "
        "your bank could not be used."
    ) in disclosure
    assert "receive a secure payment link, or check with your bank first?" in disclosure
    assert llm.calls == []  # clear yes/no answers never needed the model
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        assert session is not None and session.conversation_stage.value == "discussion"  # type: ignore[union-attr]
        assert session.transcript_consent.value == "disclosed"  # type: ignore[union-attr]
        assert session.recovery_outcome.value == "failure_explained"  # type: ignore[union-attr]


async def test_identity_not_confirmed_ends_without_details(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await answer(api, call)
    response = (await say(api, call, 1, "Sorry, wrong number")).text
    assert policy.GOODBYE_NOT_THEM in response and "<Hangup" in response
    assert "799" not in response and "Alex" not in response
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        assert session is not None and session.recovery_outcome.value == "wrong_person"  # type: ignore[union-attr]


async def test_final_reply_replays_after_the_conversation_ended(api: httpx.AsyncClient) -> None:
    """A redirect (after a deferred reply) for the turn that ended the call gets the goodbye,
    not a silent hangup; a mismatched CallSid is still rejected."""
    _, call = await approved(api)
    await answer(api, call)
    first = (await say(api, call, 1, "Sorry, wrong number")).text
    replay = (await say(api, call, 1, None)).text
    assert replay == first and policy.GOODBYE_NOT_THEM in replay and "<Hangup" in replay
    forged = await post_signed(
        api, speech_path(call["call_session_id"], 1), {"CallSid": "CA" + "f" * 32}
    )
    assert policy.GOODBYE_NOT_THEM not in forged.text and "<Hangup" in forged.text


async def test_ambiguous_identity_answer_uses_model_and_clarifies(
    api: httpx.AsyncClient, llm: FakeLLM
) -> None:
    _, call = await approved(api)
    await answer(api, call)
    llm.responses.append(model("ASK_QUESTION", "It's about your account balance of 799."))
    response = (await say(api, call, 1, "Who is calling?")).text
    assert "May I speak with Test Customer 2?" in response and "799" not in response  # scripted
    system = llm.calls[0][0].content
    assert "Do NOT mention any payment details" in system and "799" not in system


class GatedLLM(FakeLLM):
    """Answers only once `release` is set, like a model slower than Twilio's webhook timeout."""

    def __init__(self, *responses: str) -> None:
        super().__init__(*responses)
        self.release = asyncio.Event()

    async def complete(self, messages: list, *, max_tokens: int, temperature: float = 0.0) -> str:
        await self.release.wait()
        return await super().complete(messages, max_tokens=max_tokens, temperature=temperature)


async def test_slow_model_gets_hold_then_redirect_replays_reply(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    slow = GatedLLM(model("ASK_QUESTION", "It's about your account."))
    settings = voice_settings(
        tmp_path, voice_llm_timeout_seconds=5, voice_webhook_reply_within_seconds=0.05
    )
    async for api in app_client(settings, slow, MockTelephonyProvider()):
        _, call = await approved(api)
        await answer(api, call)
        held = (await say(api, call, 1, "Who is calling?")).text
        assert "<Redirect" in held and "turn=1" in held and "<Gather" not in held
        assert "<Redirect" in (await say(api, call, 1, None)).text  # still deciding: hold again
        slow.release.set()
        for _ in range(50):  # let the owned task finish and store the agent turn
            if await count(session_factory, ConversationTurn, ConversationTurn.sequence == 2):
                break
            await asyncio.sleep(0.02)
        replay = (await say(api, call, 1, None)).text  # Twilio's redirect: no SpeechResult
        assert "May I speak with Test Customer 2?" in replay and "<Gather" in replay
        assert len(slow.calls) == 1


async def test_busy_customer_gets_callback_option(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await answer(api, call)
    await say(api, call, 1, "Yes, speaking")
    busy = (await say(api, call, 2, "I'm busy right now")).text
    assert "When would be a better time" in busy and "799" not in busy
    tomorrow = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)).date()
    llm.responses.append(model("REQUEST_CALLBACK", "Sure.", callback_time=f"{tomorrow}T12:00"))
    booked = (await say(api, call, 3, "Tomorrow at noon")).text
    assert "I've booked a callback for" in booked and "<Hangup" in booked
    assert await count(session_factory, CallbackRequest) == 1


async def test_declined_assistance_is_respected(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await answer(api, call)
    await say(api, call, 1, "Yes, speaking")
    response = (await say(api, call, 2, "Not interested, thanks")).text
    assert policy.GOODBYE_DECLINED in response and "799" not in response
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        assert session is not None and session.recovery_outcome.value == "customer_declined"  # type: ignore[union-attr]


# --- Intents in discussion ----------------------------------------------------------------------


async def test_payment_link_request_creates_simulated_link_once(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(
        model(
            "REQUEST_PAYMENT_LINK",
            "Sure.",
            resolution_preference="PAYMENT_LINK",
            requires_backend_action=True,
            backend_action="request_payment_link",
        )
    )
    first = (await say(api, call, 3, "Please send me a payment link")).text
    second = (await say(api, call, 3, "Please send me a payment link")).text  # Twilio retry
    assert first == second and "created a secure payment link" in first
    assert "won't take a real payment" in first and "http" not in first.replace(
        "https://example.invalid", ""
    )
    assert len(llm.calls) == 1 and await count(session_factory, PaymentLink) == 1
    async with session_factory() as s:
        installment = await s.get(BillingInstallment, case["installment_id"])
        turn = await s.scalar(
            select(ConversationTurn).where(
                ConversationTurn.call_session_id == call["call_session_id"],
                ConversationTurn.sequence == 5,
            )
        )
        assert installment is not None and installment.payment_status.value == "failed"
        assert turn is not None and turn.intent.value == "REQUEST_PAYMENT_LINK"  # type: ignore[union-attr]
        assert turn.resolution_preference.value == "PAYMENT_LINK"  # type: ignore[union-attr]
        assert turn.backend_action == "request_payment_link" and turn.action_status == "succeeded"


async def test_already_paid_claim_requests_check_and_changes_nothing(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("ALREADY_PAID", "Great, your payment has been received!"))
    response = (await say(api, call, 3, "I already paid this yesterday")).text
    assert "asked our team to check" in response and "still shows as unpaid" in response
    assert "has been received" not in response
    async with session_factory() as s:
        note = await s.scalar(
            select(CustomerNote).where(CustomerNote.customer_id == case["customer_id"])
        )
        installment = await s.get(BillingInstallment, case["installment_id"])
        assert note is not None and "Status check requested" in note.note_text
        assert installment is not None and installment.payment_status.value == "failed"


async def test_dispute_stops_persuasion_and_records_concern(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("DISPUTE_PAYMENT", "Okay."))
    response = (await say(api, call, 3, "I never authorised this charge")).text
    assert "recorded your concern" in response and "<Hangup" in response
    async with session_factory() as s:
        note = await s.scalar(
            select(CustomerNote).where(CustomerNote.customer_id == case["customer_id"])
        )
        session = await s.get(CallSession, call["call_session_id"])
        assert note is not None and note.note_type.value == "support_context"
        assert session is not None and session.recovery_outcome.value == "escalated_to_human"  # type: ignore[union-attr]


async def test_human_support_is_recorded_honestly(api: httpx.AsyncClient, llm: FakeLLM) -> None:
    _, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("REQUEST_HUMAN_SUPPORT", "Transferring you now."))
    response = (await say(api, call, 3, "Can I talk to a real person?")).text
    assert "recorded a request for our support team" in response
    assert "can't transfer this call" in response and "Transferring" not in response


async def test_retry_request_is_informational_only(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api, "INSUFFICIENT_FUNDS", target="2026-10-05T12:00:00+05:30")
    disclosure = await to_discussion(api, call)
    assert "the bank reported insufficient funds" in disclosure
    assert "wait for the automatic retry" in disclosure
    attempts = await count(
        session_factory, PaymentAttempt, PaymentAttempt.installment_id == case["installment_id"]
    )
    llm.responses.append(model("RETRY_PAYMENT", "Retrying now!"))
    response = (await say(api, call, 3, "Can you retry the payment?")).text
    assert "already scheduled for 6 October" in response and "can't start a retry" in response
    assert (
        await count(
            session_factory, PaymentAttempt, PaymentAttempt.installment_id == case["installment_id"]
        )
        == attempts
    )


async def test_questions_get_guarded_model_answers(api: httpx.AsyncClient, llm: FakeLLM) -> None:
    _, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("ASK_QUESTION", "It failed because the mandate was inactive."))
    good = (await say(api, call, 3, "Why did it fail?")).text
    assert "It failed because the mandate was inactive." in good
    llm.responses.append(model("ASK_QUESTION", "Just read me your OTP and I'll fix it."))
    bad = (await say(api, call, 4, "How do I fix it?")).text
    assert "OTP" not in bad and "secure payment link" in bad
    discussion_prompt = llm.calls[0][0].content
    assert "799 rupees" in discussion_prompt and "+91" not in discussion_prompt


async def test_model_cannot_trigger_unsupported_actions(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(
        model(
            "ASK_QUESTION",
            "Sure.",
            requires_backend_action=True,
            backend_action="request_payment_link",
            end_call=False,
        )
    )
    await say(api, call, 3, "Hmm, okay")
    assert await count(session_factory, PaymentLink) == 0
    llm.responses.append(
        '{"spoken_response": "x", "intent": "ASK_QUESTION", "backend_action": "transfer_money"}'
    )
    llm.responses.append(model("ASK_QUESTION", "Happy to help."))  # valid re-ask
    reasked = (await say(api, call, 4, "What else?")).text
    assert "Happy to help." in reasked
    assert "Your previous reply was not valid" in llm.calls[-1][-1].content


async def test_unclear_answer_gets_one_clarification(api: httpx.AsyncClient, llm: FakeLLM) -> None:
    _, call = await approved(api)
    await to_discussion(api, call)
    llm.responses += [model("UNCLEAR_RESPONSE", "?"), model("UNCLEAR_RESPONSE", "?")]
    first = (await say(api, call, 3, "mmm the thing")).text
    second = (await say(api, call, 4, "uhh")).text
    assert "didn't quite catch that" in first and "<Gather" in first
    assert policy.GOODBYE_UNCLEAR in second and "<Hangup" in second


async def test_do_not_call_in_discussion_suppresses_contact(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    response = (await say(api, call, 3, "Remove my number from your list")).text
    assert "We won't call you again" in response and llm.calls == []
    async with session_factory() as s:
        customer = await s.get(Customer, case["customer_id"])
        assert customer is not None and customer.do_not_contact
    queue = (await api.get("/api/v1/recovery/cases", params={"simulation_id": SIM})).json()
    assert {c["status"] for c in queue["items"] if c["customer_id"] == case["customer_id"]} == {
        "opted_out"
    }


async def test_contact_suppressed_mid_call_ends_the_call(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await answer(api, call)
    async with session_factory() as s, s.begin():
        customer = await s.get(Customer, case["customer_id"])
        assert customer is not None
        customer.do_not_contact = True
    response = (await say(api, call, 1, "Yes, speaking")).text
    assert conv.OPTED_OUT_GOODBYE in response and "Alex" not in response


async def test_payment_confirmed_mid_call_ends_without_persuasion(
    api: httpx.AsyncClient, llm: FakeLLM
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    injected = await api.post(
        f"/api/v1/installments/{case['installment_id']}/inject-scenario",
        json={"scenario_id": "ALREADY_PAID", "reason": "bank confirmed"},
    )
    assert injected.json()["payment_status"] == "success"
    response = (await say(api, call, 3, "So what should I do?")).text
    assert "payment is complete" in response and "<Hangup" in response and llm.calls == []


async def test_no_conversation_without_an_approved_call(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    await api.post("/api/v1/simulation", json={"simulation_id": SIM})
    await api.post(
        "/api/v1/simulation/advance",
        json={"simulation_id": SIM, "target_time": "2026-10-10T12:00:00+05:30"},
    )
    assert await count(session_factory, CallSession) == 0  # queued cases are never dialled
