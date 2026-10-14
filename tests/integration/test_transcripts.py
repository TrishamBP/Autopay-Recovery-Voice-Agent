"""Persistent transcripts, call events, retrieval APIs, redaction and retention."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.audit.models import AuditLog
from src.calls.models import CallEvent, CallSession, ConversationTurn
from src.calls.transcripts import purge_expired_transcripts
from src.common.time import utc_now
from src.llm.client import ChatMessage
from src.telephony.mock import MockTelephonyProvider
from src.voice import conversation as conv
from tests.conftest import FakeLLM
from tests.integration.test_conversation_engine import approved, model, to_discussion
from tests.integration.test_twilio_voice import (
    answer,
    app_client,
    post_signed,
    say,
    voice_settings,
)
from tests.sim_fixtures import seed_customers, three_customers

Factory = async_sessionmaker[AsyncSession]


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


async def turns(factory: Factory, call_id: str) -> list[ConversationTurn]:
    async with factory() as s:
        return list(
            await s.scalars(
                select(ConversationTurn)
                .where(ConversationTurn.call_session_id == call_id)
                .order_by(ConversationTurn.sequence)
            )
        )


async def events(factory: Factory, call_id: str) -> list[str]:
    async with factory() as s:
        rows = await s.scalars(
            select(CallEvent)
            .where(CallEvent.call_session_id == call_id)
            .order_by(CallEvent.occurred_at, CallEvent.id)
        )
        return [e.event_type.value for e in rows]


# --- Persistence ---------------------------------------------------------------------------------


async def test_turns_persist_in_order_with_trusted_links(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("ASK_QUESTION", "It was declined by the bank."))
    await say(api, call, 3, "Why? My card 4111 1111 1111 1111 should work, OTP is 482193")
    rows = await turns(session_factory, call["call_session_id"])
    assert [(t.sequence, t.role.value) for t in rows] == [
        (0, "agent"),
        (1, "customer"),
        (2, "agent"),
        (3, "customer"),
        (4, "agent"),
        (5, "customer"),
        (6, "agent"),
    ]
    assert {t.recovery_case_id for t in rows} == {case["recovery_case_id"]}
    assert {t.customer_id for t in rows} == {case["customer_id"]}
    assert rows[1].transcript_text == conv.WITHHELD_PRE_NOTICE  # before the transcript notice
    assert rows[3].transcript_text == "Sure, go ahead"
    assert "4111" not in rows[5].transcript_text and "482193" not in rows[5].transcript_text
    assert "[REDACTED_CARD]" in rows[5].transcript_text and rows[5].original_text is None
    assert rows[5].intent.value == "ASK_QUESTION"  # type: ignore[union-attr]
    assert rows[5].processing_status.value == "processed"  # type: ignore[union-attr]
    assert rows[5].received_at is not None and rows[5].processed_at is not None
    assert rows[1].intent.value == "IDENTITY_CONFIRMED"  # type: ignore[union-attr]
    # Delivery: earlier prompts were followed by another Twilio callback; the last was not.
    assert rows[4].delivery_status.value == "played"  # type: ignore[union-attr]
    assert rows[6].delivery_status.value == "returned"  # type: ignore[union-attr]
    assert len({t.idempotency_key for t in rows}) == len(rows)


async def test_original_text_policy_still_strips_secrets(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    llm = FakeLLM(model("ASK_QUESTION", "Okay."))
    async for http in app_client(
        voice_settings(tmp_path, transcript_store_original=True), llm, MockTelephonyProvider()
    ):
        _, call = await approved(http)
        await to_discussion(http, call)
        await say(http, call, 3, "my pin is 4321, call me on +91 98765 43210")
    customer_turn = (await turns(session_factory, call["call_session_id"]))[5]
    assert customer_turn.original_text is not None and "4321 " not in customer_turn.original_text
    assert "[REDACTED_SECRET]" in customer_turn.original_text
    assert "[REDACTED_PHONE]" in customer_turn.transcript_text


async def test_metadata_only_policy_keeps_no_customer_text(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    await seed_customers(session_factory, three_customers())
    async for http in app_client(
        voice_settings(tmp_path, transcript_policy="metadata_only"),
        FakeLLM(),
        MockTelephonyProvider(),
    ):
        _, call = await approved(http)
        await to_discussion(http, call)
        body = (await http.get(f"/api/v1/calls/{call['call_session_id']}/transcript")).json()
    customer = [t for t in body["turns"] if t["speaker"] == "CUSTOMER"]
    assert {t["text"] for t in customer} == {conv.WITHHELD_POLICY}
    assert body["transcript_status"] == "metadata_only"
    assert customer[1]["intent"] == "AGREE_TO_CONTINUE"


async def test_duplicate_delivery_creates_no_duplicate_turns(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await answer(api, call)
    await answer(api, call)  # duplicate /voice
    for _ in range(3):
        await say(api, call, 1, "Yes, speaking")
    rows = await turns(session_factory, call["call_session_id"])
    assert [t.sequence for t in rows] == [0, 1, 2]
    names = await events(session_factory, call["call_session_id"])
    assert names.count("CONVERSATION_STARTED") == 1
    assert names.count("CONVERSATION_TURN_RECEIVED") == 1


async def test_concurrent_submissions_do_not_conflict(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    class SlowOnce(FakeLLM):
        async def complete(
            self, messages: list[ChatMessage], *, max_tokens: int, temperature: float = 0.0
        ) -> str:
            await asyncio.sleep(0.1)  # within the 0.2 s test budget
            return await super().complete(messages, max_tokens=max_tokens)

    await seed_customers(session_factory, three_customers())
    llm = SlowOnce(model("ASK_QUESTION", "Here you go."), model("ASK_QUESTION", "Here you go."))
    async for http in app_client(voice_settings(tmp_path), llm, MockTelephonyProvider()):
        _, call = await approved(http)
        await to_discussion(http, call)
        results = await asyncio.gather(*(say(http, call, 3, "What happened?") for _ in range(3)))
    texts = [r.text for r in results]
    assert sum("Here you go." in t for t in texts) >= 1
    assert all("Here you go." in t or "<Redirect" in t for t in texts)  # others hold & retry
    rows = await turns(session_factory, call["call_session_id"])
    assert [t.sequence for t in rows] == [0, 1, 2, 3, 4, 5, 6]
    assert len(llm.calls) == 1


async def test_llm_timeout_is_recorded_with_its_fallback(
    tmp_path: Path, engine: AsyncEngine, session_factory: Factory
) -> None:
    from tests.integration.test_twilio_voice import SlowLLM

    await seed_customers(session_factory, three_customers())
    async for http in app_client(
        voice_settings(tmp_path, voice_llm_timeout_seconds=0.05), SlowLLM(), MockTelephonyProvider()
    ):
        _, call = await approved(http)
        await answer(http, call)
        await say(http, call, 1, "Hmm, who did you say?")
    rows = await turns(session_factory, call["call_session_id"])
    assert rows[1].processing_status.value == "fallback"  # type: ignore[union-attr]
    assert rows[1].error_category == "llm_timeout"
    assert rows[2].transcript_text == conv.FALLBACK  # the customer turn was NOT lost
    assert "LLM_FALLBACK_USED" in await events(session_factory, call["call_session_id"])


async def test_action_success_and_failure_are_recorded(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    _, call = await approved(api)
    await to_discussion(api, call)
    llm.responses.append(model("REQUEST_PAYMENT_LINK", "Sure."))
    await say(api, call, 3, "Send me a link")
    llm.responses.append(model("REQUEST_CALLBACK", "Okay.", callback_time="2020-01-01T10:00"))
    await say(api, call, 4, "Call me back on 1 January 2020")  # in the past -> rejected
    rows = await turns(session_factory, call["call_session_id"])
    link_turn, callback_turn = rows[5], rows[7]
    assert (link_turn.backend_action, link_turn.action_status) == (
        "request_payment_link",
        "succeeded",
    )
    assert (callback_turn.backend_action, callback_turn.action_status) == (
        "schedule_callback",
        "rejected",
    )
    assert callback_turn.action_result["error_category"] == "business_rule_violation"  # type: ignore[index]
    assert "couldn't book" in rows[8].transcript_text
    names = await events(session_factory, call["call_session_id"])
    assert names.count("BACKEND_ACTION_SUCCEEDED") == 1
    assert names.count("BACKEND_ACTION_FAILED") == 1
    assert "CUSTOMER_REQUESTED_CALLBACK" in names


async def test_twilio_callbacks_create_idempotent_events_without_regression(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved(api)
    path = f"/api/v1/twilio/status?call_session_id={call['call_session_id']}"
    sid = call["provider_call_id"]
    for status, seq in [
        ("ringing", "1"),
        ("in-progress", "2"),
        ("completed", "4"),
        ("completed", "4"),
        ("ringing", "3"),
    ]:
        await post_signed(api, path, {"CallSid": sid, "CallStatus": status, "SequenceNumber": seq})
    names = await events(session_factory, call["call_session_id"])
    assert names.count("CALL_RINGING") == 1 and names.count("CALL_COMPLETED") == 1
    assert names.count("CALL_ANSWERED") == 1 and names.count("CONVERSATION_ENDED") == 1
    assert names[:3] == ["CALL_APPROVED", "CALL_INITIATION_REQUESTED", "CALL_INITIATED"]
    async with session_factory() as s:
        session = await s.get(CallSession, call["call_session_id"])
        assert session is not None and session.status.value == "completed"


async def test_persistence_failure_is_explicit(
    api: httpx.AsyncClient, session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, call = await approved(api)
    await answer(api, call)

    async def broken(self: object, ctx: object, reply: object) -> None:
        raise SQLAlchemyError("disk full")

    monkeypatch.setattr(conv.VoiceConversationService, "_finish", broken)
    response = (await say(api, call, 1, "Yes, speaking")).text
    assert conv.TECHNICAL_GOODBYE in response and "<Hangup" in response
    rows = await turns(session_factory, call["call_session_id"])
    assert [t.sequence for t in rows] == [0, 1]  # no agent turn claimed
    assert rows[1].processing_status.value == "failed"  # type: ignore[union-attr]
    assert rows[1].error_category == "persistence_error"


# --- Retrieval APIs ----------------------------------------------------------------------------


async def test_transcript_audit_list_and_export(
    api: httpx.AsyncClient, llm: FakeLLM, session_factory: Factory
) -> None:
    case, call = await approved(api)
    call_id = call["call_session_id"]
    await to_discussion(api, call)
    llm.responses.append(model("ASK_QUESTION", "Sure."))
    await say(api, call, 3, "email me at someone@example.com")

    transcript = (await api.get(f"/api/v1/calls/{call_id}/transcript")).json()
    assert transcript["call_id"] == call_id and transcript["transcript_status"] == "available"
    assert transcript["recovery_case_id"] == case["recovery_case_id"]
    assert transcript["twilio_call_sid"] == call["provider_call_id"]
    speakers = [t["speaker"] for t in transcript["turns"]]
    assert speakers[:3] == ["AGENT", "CUSTOMER", "AGENT"]
    assert "[REDACTED_EMAIL]" in transcript["turns"][5]["text"]
    assert "original_text" not in json.dumps(transcript)

    audit = (await api.get(f"/api/v1/calls/{call_id}/audit")).json()
    types = [e["event_type"] for e in audit["events"]]
    assert "CALL_APPROVED" in types and "CONVERSATION_TURN_RECEIVED" in types

    listing = (
        await api.get("/api/v1/calls", params={"customer_id": case["customer_id"], "limit": 5})
    ).json()
    assert listing["total"] == 1 and listing["items"][0]["turn_count"] == 7
    assert listing["items"][0]["scenario_id"] == "MANDATE_INACTIVE"
    assert "turns" not in listing["items"][0]
    assert (await api.get("/api/v1/calls", params={"scenario_id": "BANK_TIMEOUT"})).json()[
        "total"
    ] == 0
    assert (await api.get("/api/v1/calls", params={"limit": 500})).status_code == 422

    export = await api.get(f"/api/v1/calls/{call_id}/transcript/export")
    assert export.headers["content-disposition"].startswith("attachment")
    exported = export.json()
    assert "someone@example.com" not in export.text
    assert exported["transcript"]["call_id"] == call_id and exported["audit"]["events"]
    async with session_factory() as s:
        accesses = (
            await s.scalars(select(AuditLog.event_type).where(AuditLog.entity_id == call_id))
        ).all()
        assert "transcript.accessed" in accesses and "transcript.exported" in accesses


async def test_transcript_endpoints_require_authorization(api: httpx.AsyncClient) -> None:
    _, call = await approved(api)
    for path in (
        f"/api/v1/calls/{call['call_session_id']}/transcript",
        f"/api/v1/calls/{call['call_session_id']}/audit",
        "/api/v1/calls",
        f"/api/v1/calls/{call['call_session_id']}/transcript/export",
    ):
        assert (await api.get(path, headers={"X-API-Key": "wrong"})).status_code == 401
    missing = await api.get("/api/v1/calls/00000000-0000-0000-0000-000000000000/transcript")
    assert missing.status_code == 404


async def test_retention_purge_removes_text_but_keeps_audit(
    api: httpx.AsyncClient, session_factory: Factory
) -> None:
    _, call = await approved(api)
    call_id = call["call_session_id"]
    await answer(api, call)
    await say(api, call, 1, "Yes, speaking")
    async with session_factory() as s, s.begin():
        session = await s.get(CallSession, call_id)
        assert session is not None
        session.started_at = utc_now() - timedelta(days=40, minutes=5)
        session.ended_at = utc_now() - timedelta(days=40)
    planned = await purge_expired_transcripts(
        session_factory, now=utc_now(), retention_days=30, dry_run=True
    )
    assert planned == [call_id] and len(await turns(session_factory, call_id)) == 3
    assert await purge_expired_transcripts(session_factory, now=utc_now(), retention_days=30) == [
        call_id
    ]
    assert await turns(session_factory, call_id) == []
    async with session_factory() as s:
        events_left = await s.scalar(
            select(func.count()).select_from(CallEvent).where(CallEvent.call_session_id == call_id)
        )
        assert events_left and events_left > 0
    body = (await api.get(f"/api/v1/calls/{call_id}/transcript")).json()
    assert body["transcript_status"] == "purged" and body["turns"] == []
    assert (
        await purge_expired_transcripts(session_factory, now=utc_now(), retention_days=30) == []
    )  # idempotent
