"""Twilio voice webhooks, conversation turns, outbound adapter and reconciliation.

Twilio and Fireworks are mocked: signatures are computed with the official SDK validator using a
fake token; the model is a scripted FakeLLM. No network, no credentials, no real calls.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from twilio.request_validator import RequestValidator

from src.calls.models import CallSession, ConversationTurn
from src.config import Settings
from src.customers.models import Customer
from src.llm.client import ChatMessage, LLMProviderError
from src.main import create_app
from src.recovery.models import RecoveryCase
from src.telephony.base import TelephonyError, TelephonyOutcomeUnknownError
from src.telephony.factory import create_provider
from src.telephony.mock import MockTelephonyProvider
from src.telephony.twilio import TwilioTelephonyProvider
from src.voice import conversation as conv
from tests.conftest import OPERATOR_KEY, FakeLLM, make_settings
from tests.sim_fixtures import seed_customers, three_customers

TOKEN = "test-auth-token"
BASE = "https://example.invalid"
DEST = "+15005550006"
SIM = "voice-001"
APPROVAL = {
    "approved": True,
    "approved_by": "Test Operator",
    "approval_reason": "Voice test",
    "test_destination": DEST,
}
Factory = async_sessionmaker[AsyncSession]


class SlowLLM(FakeLLM):
    async def complete(
        self, messages: list[ChatMessage], *, max_tokens: int, temperature: float = 0.0
    ) -> str:
        self.calls.append(messages)
        await asyncio.sleep(5)
        return "{}"


def voice_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "twilio_auth_token": TOKEN,
        "public_base_url": BASE,
        "voice_llm_timeout_seconds": 0.2,
    }
    return make_settings(tmp_path, **(values | overrides))


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def mock() -> MockTelephonyProvider:
    return MockTelephonyProvider()


async def app_client(
    settings: Settings, llm: FakeLLM, mock: MockTelephonyProvider
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, llm_client=llm, telephony=mock)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": OPERATOR_KEY},
        ) as http,
    ):
        yield http


@pytest.fixture
async def api(
    tmp_path: Path,
    engine: AsyncEngine,
    session_factory: Factory,
    llm: FakeLLM,
    mock: MockTelephonyProvider,
) -> AsyncIterator[httpx.AsyncClient]:
    await seed_customers(session_factory, three_customers())
    async for http in app_client(voice_settings(tmp_path), llm, mock):
        yield http


async def queued_case(api: httpx.AsyncClient) -> dict:
    await api.post(
        "/api/v1/simulation",
        json={
            "simulation_id": SIM,
            "scenario_assignments": [
                {"subscription_id": "SUB-3001", "scenario_id": "SUCCESSFUL_PAYMENT"},
                {"subscription_id": "SUB-3002", "scenario_id": "MANDATE_INACTIVE"},
                {"subscription_id": "SUB-3003", "scenario_id": "SUCCESSFUL_PAYMENT"},
            ],
        },
    )
    await api.post(
        "/api/v1/simulation/advance",
        json={"simulation_id": SIM, "target_time": "2026-10-10T12:00:00+05:30"},
    )
    queue = (await api.get("/api/v1/recovery/queue", params={"simulation_id": SIM})).json()
    return queue["items"][0]


async def approved_call(api: httpx.AsyncClient) -> tuple[dict, dict]:
    case = await queued_case(api)
    approved = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert approved.status_code == 200, approved.text
    return case, approved.json()


def sign(path: str, form: dict[str, str], token: str = TOKEN) -> str:
    return RequestValidator(token).compute_signature(f"{BASE}{path}", form)


async def post_signed(api: httpx.AsyncClient, path: str, form: dict[str, str]) -> httpx.Response:
    return await api.post(path, data=form, headers={"X-Twilio-Signature": sign(path, form)})


def voice_path(call_id: str) -> str:
    return f"/api/v1/twilio/voice?call_session_id={call_id}"


def speech_path(call_id: str, turn: int) -> str:
    return f"/api/v1/twilio/process-speech?call_session_id={call_id}&turn={turn}"


async def answer(api: httpx.AsyncClient, call: dict) -> httpx.Response:
    return await post_signed(
        api,
        voice_path(call["call_session_id"]),
        {"CallSid": call["provider_call_id"], "CallStatus": "in-progress"},
    )


async def say(api: httpx.AsyncClient, call: dict, turn: int, speech: str | None) -> httpx.Response:
    form = {"CallSid": call["provider_call_id"]}
    if speech is not None:
        form |= {"SpeechResult": speech, "Confidence": "0.91"}
    return await post_signed(api, speech_path(call["call_session_id"], turn), form)


async def count(factory: Factory, model: type, *where: object) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where)) or 0


# --- Webhook security -------------------------------------------------------------------------


async def test_signature_required_and_verified(api: httpx.AsyncClient) -> None:
    _, call = await approved_call(api)
    path, form = voice_path(call["call_session_id"]), {"CallSid": call["provider_call_id"]}
    assert (await api.post(path, data=form)).status_code == 403  # missing
    forged = await api.post(path, data=form, headers={"X-Twilio-Signature": sign(path, form, "x")})
    assert forged.status_code == 403
    tampered = await api.post(
        path, data=form | {"CallSid": "CAother"}, headers={"X-Twilio-Signature": sign(path, form)}
    )
    assert tampered.status_code == 403
    spoofed = await api.post(
        path,
        data=form,
        headers={
            "X-Twilio-Signature": RequestValidator(TOKEN).compute_signature(
                f"https://attacker.example{path}", form
            ),
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "https",
        },
    )
    assert spoofed.status_code == 403  # forwarded headers are not trusted
    assert (await answer(api, call)).status_code == 200


async def test_unconfigured_webhooks_return_503(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    async for http in app_client(make_settings(tmp_path), FakeLLM(), MockTelephonyProvider()):
        response = await http.post(
            "/api/v1/twilio/voice?call_session_id=abc", data={"CallSid": "CA1"}
        )
        assert response.status_code == 503


async def test_unsigned_dev_mode_is_explicit_and_mock_only(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    mock = MockTelephonyProvider()
    settings = make_settings(
        tmp_path, twilio_allow_unsigned_webhooks=True, public_base_url="http://127.0.0.1:8000"
    )
    async for http in app_client(settings, FakeLLM(), mock):
        _, call = await approved_call(http)
        response = await http.post(
            voice_path(call["call_session_id"]), data={"CallSid": call["provider_call_id"]}
        )
        assert response.status_code == 200 and "may I speak with" in response.text
        health = (await http.get("/api/v1/twilio/health")).json()
        assert health["unsigned_webhooks_allowed"] is True


async def test_unverified_sessions_get_a_bare_hangup(api: httpx.AsyncClient) -> None:
    _, call = await approved_call(api)
    wrong_sid = await post_signed(api, voice_path(call["call_session_id"]), {"CallSid": "CAfake"})
    unknown = await post_signed(
        api,
        voice_path("00000000-0000-0000-0000-000000000000"),
        {"CallSid": call["provider_call_id"]},
    )
    no_sid = await post_signed(api, voice_path(call["call_session_id"]), {})
    for response in (wrong_sid, unknown, no_sid):
        assert response.status_code == 200
        assert "<Hangup" in response.text and "<Say" not in response.text


# --- Conversation transport (behaviour is covered in test_conversation_engine.py) -----------


async def test_answer_returns_opening_with_absolute_gather_url(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved_call(api)
    greeting = await answer(api, call)
    assert greeting.headers["content-type"].startswith("application/xml")
    assert "Hello, may I speak with Test Customer 2?" in greeting.text
    assert 'language="en-IN"' in greeting.text and "rupees" not in greeting.text
    assert (
        f'action="{BASE}/api/v1/twilio/process-speech?call_session_id='
        f'{call["call_session_id"]}&amp;turn=1"'
    ) in greeting.text
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        assert session is not None and session.status.value == "in_progress"
        assert session.started_at is not None
    assert await count(session_factory, ConversationTurn) == 1


async def test_silence_and_missing_speech_reprompt_then_end(
    api: httpx.AsyncClient, llm: FakeLLM
) -> None:
    _, call = await approved_call(api)
    await answer(api, call)
    first = await say(api, call, 1, "")
    second = await say(api, call, 2, None)  # SpeechResult missing entirely
    third = await say(api, call, 3, "   ")
    assert conv.SILENCE_PROMPT in first.text and "<Gather" in first.text
    assert conv.SILENCE_PROMPT in second.text
    assert conv.SILENCE_GOODBYE in third.text and "<Hangup" in third.text
    assert llm.calls == []  # silence never reaches the model


async def test_glm_timeout_falls_back_then_ends(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    slow = SlowLLM()
    async for http in app_client(
        voice_settings(tmp_path, voice_llm_timeout_seconds=0.05), slow, MockTelephonyProvider()
    ):
        _, call = await approved_call(http)
        await answer(http, call)
        first = await say(http, call, 1, "Hmm, who did you say?")  # needs the model
        assert conv.FALLBACK in first.text and "<Gather" in first.text
        second = await say(http, call, 2, "What is this about?")
        assert conv.TECHNICAL_GOODBYE in second.text and "<Hangup" in second.text
    assert len(slow.calls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        '{"intent": "ASK_QUESTION"}',
        '{"spoken_response": "hi", "intent": "PAY_NOW"}',
        LLMProviderError("x"),
    ],
)
async def test_malformed_or_failed_model_output_falls_back(
    api: httpx.AsyncClient, llm: FakeLLM, bad: object
) -> None:
    _, call = await approved_call(api)
    await answer(api, call)
    llm.responses += [bad, bad]  # type: ignore[list-item]  # the re-ask fails too
    response = await say(api, call, 1, "Hmm, who did you say?")
    assert conv.FALLBACK in response.text
    assert "Traceback" not in response.text and "PAY_NOW" not in response.text


async def test_opt_out_is_enforced_without_the_model(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved_call(api)
    await answer(api, call)
    response = await say(api, call, 1, "Please stop calling me")
    assert "We won't call you again" in response.text and "<Hangup" in response.text
    assert llm.calls == []
    async with session_factory() as s:
        customer = await s.get(Customer, case["customer_id"])
        recovery_case = await s.get(RecoveryCase, case["recovery_case_id"])
        session = await s.get(CallSession, call["call_session_id"])
        assert customer is not None and customer.do_not_contact is True
        assert recovery_case is not None and recovery_case.status.value == "opted_out"
        assert session is not None
        assert session.recovery_outcome.value == "do_not_contact_requested"  # type: ignore[union-attr]
    again = await answer(api, call)
    assert "<Say" not in again.text  # session no longer conversable


async def test_duplicate_webhook_replays_and_stale_turn_is_rejected(
    api: httpx.AsyncClient, llm: FakeLLM
) -> None:
    _, call = await approved_call(api)
    await answer(api, call)
    first = await say(api, call, 1, "Yes, speaking")
    second = await say(api, call, 1, "Yes, speaking")
    assert first.text == second.text and "my name is Alex" in first.text
    stale = await say(api, call, 5, "skip ahead")
    assert "<Say" not in stale.text and "<Hangup" in stale.text


async def test_status_callbacks_update_session_idempotently(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    case, call = await approved_call(api)
    path = f"/api/v1/twilio/status?call_session_id={call['call_session_id']}"
    sid = call["provider_call_id"]
    for form in (
        {"CallSid": sid, "CallStatus": "in-progress", "SequenceNumber": "1"},
        {"CallSid": sid, "CallStatus": "completed", "SequenceNumber": "3", "CallDuration": "42"},
        {"CallSid": sid, "CallStatus": "ringing", "SequenceNumber": "2"},  # late
        {"CallSid": sid, "CallStatus": "completed", "SequenceNumber": "3"},
    ):  # dup
        assert (await post_signed(api, path, form)).status_code == 204
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        recovery_case = await s.get(RecoveryCase, case["recovery_case_id"])
        assert session is not None and session.status.value == "completed"
        assert session.duration_seconds == 42 and session.ended_at is not None
        assert recovery_case is not None and recovery_case.status.value == "call_completed"
    other = await post_signed(api, path, {"CallSid": "CAother", "CallStatus": "completed"})
    assert other.status_code == 422  # SID must match the session


async def test_health_reports_status_without_secrets(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/twilio/health", headers={"X-API-Key": ""})
    body = response.json()
    assert body["telephony_provider"] == "mock" and body["live_calls_possible"] is False
    assert body["webhook_urls"]["voice"] == f"{BASE}/api/v1/twilio/voice"
    assert TOKEN not in response.text and "auth_token" not in response.text


# --- Outbound calls, approval and reconciliation ---------------------------------------------


async def test_customer_record_numbers_are_never_dialled(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    mock = MockTelephonyProvider()
    settings = voice_settings(tmp_path, test_destination_allowlist=f"{DEST},+919000009002")
    async for http in app_client(settings, FakeLLM(), mock):
        case = await queued_case(http)
        response = await http.post(
            f"/api/v1/recovery/{case['recovery_case_id']}/approve",
            json=APPROVAL | {"test_destination": "+919000009002"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "destination_is_customer_record"
    assert mock.calls == []


async def test_ambiguous_initiation_is_reconciled_before_any_retry(
    api: httpx.AsyncClient, mock: MockTelephonyProvider
) -> None:
    case = await queued_case(api)
    mock.failure_mode = "timeout_created"  # Twilio accepted, but our request timed out
    url = f"/api/v1/recovery/{case['recovery_case_id']}/approve"
    first = await api.post(url, json=APPROVAL)
    assert first.status_code == 200 and first.json()["call_initiation"] == "unknown"
    assert (await api.post(url, json=APPROVAL)).status_code == 409  # no blind re-dial
    reconciled = await api.post(
        f"/api/v1/recovery/{case['recovery_case_id']}/reconcile-call",
        json={"test_destination": DEST},
    )
    assert reconciled.status_code == 200 and reconciled.json()["status"] == "call_initiated"
    assert reconciled.json()["provider_call_id"] == mock.calls[0].provider_call_id
    assert len(mock.calls) == 1


async def test_reconcile_not_found_waits_for_grace_then_allows_new_approval(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    mock = MockTelephonyProvider(failure_mode="timeout")  # no call was created
    async for http in app_client(voice_settings(tmp_path), FakeLLM(), mock):
        case = await queued_case(http)
        base = f"/api/v1/recovery/{case['recovery_case_id']}"
        await http.post(f"{base}/approve", json=APPROVAL)
        early = await http.post(f"{base}/reconcile-call", json={"test_destination": DEST})
        assert early.status_code == 409
        assert early.json()["error"]["details"]["reason"] == "reconcile_too_early"
    async for http in app_client(
        voice_settings(tmp_path, telephony_reconcile_grace_seconds=0), FakeLLM(), mock
    ):
        done = await http.post(f"{base}/reconcile-call", json={"test_destination": DEST})
        assert done.status_code == 200 and done.json()["status"] == "call_failed"
        mock.failure_mode = "none"
        retry = await http.post(f"{base}/approve", json=APPROVAL)  # explicit new approval
        assert retry.status_code == 200 and retry.json()["status"] == "call_initiated"
    assert len(mock.calls) == 1


def live_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "telephony_provider": "twilio",
        "twilio_live_calls_enabled": True,
        "twilio_account_sid": "AC" + "0" * 32,
        "twilio_auth_token": TOKEN,
        "twilio_from_number": "+15005550001",
        "public_base_url": BASE,
    }
    return make_settings(tmp_path, **(values | overrides))


def test_live_mode_requires_complete_https_configuration(tmp_path: Path) -> None:
    assert create_provider(make_settings(tmp_path)).name == "mock"
    for broken in (
        {"twilio_live_calls_enabled": False},
        {"twilio_from_number": None},
        {"public_base_url": "http://insecure.example"},
        {"twilio_auth_token": None},
    ):
        with pytest.raises(ValueError):
            create_provider(live_settings(tmp_path, **broken))
    assert isinstance(create_provider(live_settings(tmp_path)), TwilioTelephonyProvider)


class FakeCalls:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.created, self.listed = error, [], []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.created.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(sid="CA" + "1" * 32)

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        self.listed.append(kwargs)
        return [SimpleNamespace(sid="CA" + "2" * 32)]


async def test_twilio_adapter_builds_absolute_urls_and_classifies_errors(tmp_path: Path) -> None:
    from requests.exceptions import ConnectTimeout, ReadTimeout
    from twilio.base.exceptions import TwilioRestException

    calls = FakeCalls()
    provider = TwilioTelephonyProvider(live_settings(tmp_path), client=SimpleNamespace(calls=calls))
    result = await provider.initiate_call(
        recovery_case_id="rc", call_session_id="cs-1", destination=DEST, idempotency_key="k"
    )
    assert result.provider_call_id.startswith("CA")
    sent = calls.created[0]
    assert sent["url"] == f"{BASE}/api/v1/twilio/voice?call_session_id=cs-1"
    assert sent["status_callback"] == f"{BASE}/api/v1/twilio/status?call_session_id=cs-1"
    assert sent["status_callback_event"] == ["initiated", "ringing", "answered", "completed"]
    assert sent["to"] == DEST and sent["from_"] == "+15005550001" and sent["time_limit"] == 300
    found = await provider.find_call(
        call_session_id="cs-1", destination=DEST, created_after=datetime.now(UTC)
    )
    assert found == "CA" + "2" * 32

    for error, expected in (
        (ReadTimeout(), TelephonyOutcomeUnknownError),
        (ConnectTimeout(), TelephonyError),
        (TwilioRestException(400, "uri", "bad"), TelephonyError),
    ):
        failing = TwilioTelephonyProvider(
            live_settings(tmp_path), client=SimpleNamespace(calls=FakeCalls(error))
        )
        with pytest.raises(expected):
            await failing.initiate_call(
                recovery_case_id="rc", call_session_id="cs", destination=DEST, idempotency_key="k"
            )


async def test_trial_account_omits_disallowed_call_parameters(tmp_path: Path) -> None:
    calls = FakeCalls()
    provider = TwilioTelephonyProvider(
        live_settings(tmp_path, twilio_trial_account=True), client=SimpleNamespace(calls=calls)
    )
    await provider.initiate_call(
        recovery_case_id="rc", call_session_id="cs-1", destination=DEST, idempotency_key="k"
    )
    sent = calls.created[0]
    assert set(sent) == {"to", "from_", "url"}  # trials reject every optional parameter
    assert sent["url"] == f"{BASE}/api/v1/twilio/voice?call_session_id=cs-1"


async def test_live_mode_checks_account_sid_on_webhooks(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    """A webhook for a Twilio session must carry our AccountSid."""
    await seed_customers(session_factory, three_customers())
    settings = live_settings(tmp_path)
    twilio = TwilioTelephonyProvider(settings, client=SimpleNamespace(calls=FakeCalls()))
    async for http in app_client(settings, FakeLLM(), twilio):  # type: ignore[arg-type]
        _, call = await approved_call(http)
        assert call["provider"] == "twilio"
        form = {"CallSid": call["provider_call_id"], "AccountSid": "AC" + "9" * 32}
        wrong = await post_signed(http, voice_path(call["call_session_id"]), form)
        assert "<Say" not in wrong.text
        good = await post_signed(
            http,
            voice_path(call["call_session_id"]),
            form | {"AccountSid": settings.twilio_account_sid or ""},
        )
        assert "may I speak with" in good.text


async def test_trial_unsigned_exception_requires_exact_session_match(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    """Trial sandbox numbers send unsigned webhooks: accepted only on an exact match."""
    await seed_customers(session_factory, three_customers())

    async def run(**overrides: object) -> dict[str, int | str]:
        settings = live_settings(tmp_path, twilio_trial_account=True, **overrides)
        calls = FakeCalls()
        sid = "CA" + uuid.uuid4().hex  # unique per run: call sessions survive a reset
        calls.create = lambda **kw: SimpleNamespace(sid=sid)  # type: ignore[method-assign]
        twilio = TwilioTelephonyProvider(settings, client=SimpleNamespace(calls=calls))
        results: dict[str, int | str] = {}
        async for http in app_client(settings, FakeLLM(), twilio):  # type: ignore[arg-type]
            _, call = await approved_call(http)
            path = voice_path(call["call_session_id"])
            ours = {"CallSid": call["provider_call_id"], "AccountSid": settings.twilio_account_sid}
            exact = await http.post(path, data=ours)
            results["exact"] = exact.status_code
            results["exact_text"] = exact.text
            results["wrong_sid"] = (
                await http.post(path, data=ours | {"CallSid": "CA" + "9" * 32})
            ).status_code
            results["wrong_account"] = (
                await http.post(path, data=ours | {"AccountSid": "AC" + "9" * 32})
            ).status_code
            results["bad_signature"] = (
                await http.post(path, data=ours, headers={"X-Twilio-Signature": "forged"})
            ).status_code
            results["unknown_session"] = (
                await http.post(voice_path("00000000-0000-0000-0000-000000000000"), data=ours)
            ).status_code
            await http.post(
                "/api/v1/simulation/reset", json={"simulation_id": SIM, "confirm": f"RESET {SIM}"}
            )
        return results

    allowed = await run(twilio_trial_accept_unsigned_webhooks=True)
    assert allowed["exact"] == 200 and "may I speak with" in str(allowed["exact_text"])
    for case in ("wrong_sid", "wrong_account", "bad_signature", "unknown_session"):
        assert allowed[case] == 403, case
    off = await run(twilio_trial_accept_unsigned_webhooks=False)
    assert off["exact"] == 403
    not_trial_dev = await run(twilio_trial_accept_unsigned_webhooks=True, app_env="production")
    assert not_trial_dev["exact"] == 403
