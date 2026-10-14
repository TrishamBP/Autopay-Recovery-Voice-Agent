"""API tests: approval safety, concurrency, telephony failures and the end-to-end demo flow."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.billing.models import BillingInstallment
from src.calls.models import CallSession
from src.config import Settings
from src.customers.models import Customer
from src.main import create_app
from src.telephony.mock import MockTelephonyProvider
from tests.conftest import OPERATOR_KEY, make_settings
from tests.sim_fixtures import seed_customers, three_customers

DEST = "+15005550006"
SIM = "demo-001"
MIX = [
    {"subscription_id": "SUB-3001", "scenario_id": "SUCCESSFUL_PAYMENT"},
    {"subscription_id": "SUB-3002", "scenario_id": "MANDATE_INACTIVE"},
    {"subscription_id": "SUB-3003", "scenario_id": "INSUFFICIENT_FUNDS"},
]
APPROVAL = {
    "approved": True,
    "approved_by": "Test Operator",
    "approval_reason": "Approved for controlled demo",
    "test_destination": DEST,
}


async def _client(
    settings: Settings, mock: MockTelephonyProvider
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, telephony=mock)
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
def mock() -> MockTelephonyProvider:
    return MockTelephonyProvider()


@pytest.fixture
async def api(
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    mock: MockTelephonyProvider,
) -> AsyncIterator[httpx.AsyncClient]:
    await seed_customers(session_factory, three_customers())
    async for http in _client(settings, mock):
        yield http


async def start(api: httpx.AsyncClient, target: str = "2026-10-10T12:00:00+05:30") -> dict:
    created = await api.post(
        "/api/v1/simulation",
        json={
            "simulation_id": SIM,
            "initial_time": "2026-10-01T00:00:00+05:30",
            "scenario_assignments": MIX,
        },
    )
    assert created.status_code == 201, created.text
    advanced = await api.post(
        "/api/v1/simulation/advance", json={"simulation_id": SIM, "target_time": target}
    )
    assert advanced.status_code == 200, advanced.text
    return advanced.json()


async def queue(api: httpx.AsyncClient) -> list[dict]:
    return (await api.get("/api/v1/recovery/queue", params={"simulation_id": SIM})).json()["items"]


async def case_for(api: httpx.AsyncClient, customer_id: str) -> dict:
    return next(c for c in await queue(api) if c["customer_id"] == customer_id)


async def call_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(CallSession)) or 0


# --- End-to-end (spec section 15.F) ----------------------------------------------------------


async def test_end_to_end_demo_flow(
    api: httpx.AsyncClient,
    mock: MockTelephonyProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    summary = await start(api)
    assert summary["previous_time"] == "2026-10-01T00:00:00+05:30"
    assert summary["current_time"] == "2026-10-10T12:00:00+05:30"
    assert summary["installments_created"] == 3 and summary["calls_initiated"] == 0

    installments = (await api.get("/api/v1/installments", params={"simulation_id": SIM})).json()
    assert [(i["customer_id"], i["due_at"][:10]) for i in installments["items"]] == [
        ("CUS-2001", "2026-10-02"),
        ("CUS-2002", "2026-10-04"),
        ("CUS-2003", "2026-10-06"),
    ]
    by_customer = {i["customer_id"]: i for i in installments["items"]}
    assert by_customer["CUS-2001"]["payment_status"] == "success"
    assert by_customer["CUS-2002"]["payment_status"] == "failed"
    attempts = (
        await api.get(f"/api/v1/installments/{by_customer['CUS-2003']['installment_id']}/attempts")
    ).json()
    assert [a["outcome"] for a in attempts] == ["failed", "failed", "failed"]

    events = (
        await api.get("/api/v1/simulation/events", params={"simulation_id": SIM, "limit": 500})
    ).json()["items"]
    keys = [(e["scheduled_at_virtual"], e["sequence"]) for e in events]
    assert keys == sorted(keys)

    cases = (await api.get("/api/v1/recovery/cases", params={"simulation_id": SIM})).json()["items"]
    assert sorted(c["customer_id"] for c in cases) == ["CUS-2002", "CUS-2003"]  # none for success
    assert {c["status"] for c in cases} == {"awaiting_approval"}
    assert len(await queue(api)) == 2
    assert mock.calls == [] and await call_count(session_factory) == 0  # nothing dialled yet

    case = await case_for(api, "CUS-2002")
    approved = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "call_initiated"
    assert len(mock.calls) == 1 and mock.calls[0].destination == DEST

    repeat = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert repeat.status_code == 409
    assert repeat.json()["error"]["details"]["reason"] == "case_not_awaiting_approval"
    assert len(mock.calls) == 1 and await call_count(session_factory) == 1

    detail = (await api.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
    assert detail["calls"][0]["destination"] == "+15******0006"
    assert detail["approved_by"] == "Test Operator"
    installment = (await api.get(f"/api/v1/installments/{case['installment_id']}")).json()
    assert installment["payment_status"] == "failed"  # a call never implies payment


# --- Approval safety -------------------------------------------------------------------------


async def test_idempotency_key_header_replays_approval(
    api: httpx.AsyncClient, mock: MockTelephonyProvider
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    url = f"/api/v1/recovery/{case['recovery_case_id']}/approve"
    first = await api.post(url, json=APPROVAL, headers={"Idempotency-Key": "approval-123"})
    second = await api.post(url, json=APPROVAL, headers={"Idempotency-Key": "approval-123"})
    assert first.status_code == second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["call_session_id"] == first.json()["call_session_id"]
    assert len(mock.calls) == 1


async def test_missing_consent_blocks_approval(
    tmp_path: Path, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed_customers(session_factory, three_customers())
    mock = MockTelephonyProvider()
    settings = make_settings(tmp_path, allow_synthetic_consent_for_test_destinations=False)
    async for http in _client(settings, mock):
        await start(http)
        case = await case_for(http, "CUS-2002")
        response = await http.post(
            f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "consent_invalid"
        assert (await case_for(http, "CUS-2002"))["status"] == "awaiting_approval"
    assert mock.calls == []


async def test_do_not_contact_blocks_and_opts_out(
    api: httpx.AsyncClient,
    mock: MockTelephonyProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    async with session_factory() as s, s.begin():
        customer = await s.get(Customer, "CUS-2002")
        assert customer is not None
        customer.do_not_contact = True
    response = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "do_not_contact"
    detail = (await api.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
    assert detail["status"] == "opted_out" and detail["recovery_outcome"] == "do_not_contact"
    assert mock.calls == []


@pytest.mark.parametrize("destination", ["+919000009002", "+15005550007"])
async def test_non_allowlisted_destination_is_refused(
    api: httpx.AsyncClient, mock: MockTelephonyProvider, destination: str
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    response = await api.post(
        f"/api/v1/recovery/{case['recovery_case_id']}/approve",
        json=APPROVAL | {"test_destination": destination},
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "destination_not_allowlisted"
    assert mock.calls == []


async def test_stale_approval_after_already_paid(
    api: httpx.AsyncClient, mock: MockTelephonyProvider
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    injected = await api.post(
        f"/api/v1/installments/{case['installment_id']}/inject-scenario",
        json={"scenario_id": "ALREADY_PAID", "reason": "bank confirmed"},
    )
    assert injected.status_code == 200 and injected.json()["payment_status"] == "success"
    assert injected.json()["payment_source"] == "already_paid_confirmation"
    response = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert response.status_code == 409
    detail = (await api.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
    assert detail["status"] == "cancelled" and detail["recovery_outcome"] == "already_paid"
    assert mock.calls == []
    again = await api.post(
        f"/api/v1/installments/{case['installment_id']}/inject-scenario",
        json={"scenario_id": "MANDATE_INACTIVE", "reason": "try overwrite"},
    )
    assert again.status_code == 409  # finalised outcomes cannot be overwritten


async def test_outside_contact_window_is_refused(
    api: httpx.AsyncClient, mock: MockTelephonyProvider
) -> None:
    await start(api, target="2026-10-10T21:30:00+05:30")  # after the 20:00 calling-hours limit
    case = await case_for(api, "CUS-2002")
    response = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "outside_contact_window"
    assert mock.calls == []


async def test_concurrent_approvals_place_one_call(
    api: httpx.AsyncClient,
    mock: MockTelephonyProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    url = f"/api/v1/recovery/{case['recovery_case_id']}/approve"
    responses = await asyncio.gather(*(api.post(url, json=APPROVAL) for _ in range(5)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 1 and codes.count(409) == 4
    assert len(mock.calls) == 1 and await call_count(session_factory) == 1


async def test_provider_failure_is_recorded_and_not_a_recovery(
    api: httpx.AsyncClient,
    mock: MockTelephonyProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    mock.failure_mode = "error"
    url = f"/api/v1/recovery/{case['recovery_case_id']}/approve"
    failed = await api.post(url, json=APPROVAL)
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "call_initiation_failed"
    detail = (await api.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
    assert detail["status"] == "call_failed" and detail["calls"][0]["status"] == "failed"
    async with session_factory() as s:
        installment = await s.get(BillingInstallment, case["installment_id"])
        assert installment is not None and installment.payment_status.value == "failed"
    mock.failure_mode = "none"
    retried = await api.post(url, json=APPROVAL)  # a NEW explicit approval is required
    assert retried.status_code == 200 and len(mock.calls) == 1


async def test_ambiguous_timeout_is_reconciled_by_status_updates(
    api: httpx.AsyncClient,
    mock: MockTelephonyProvider,
) -> None:
    await start(api)
    case = await case_for(api, "CUS-2002")
    mock.failure_mode = "timeout"
    url = f"/api/v1/recovery/{case['recovery_case_id']}/approve"
    response = await api.post(url, json=APPROVAL)
    assert response.status_code == 200 and response.json()["call_initiation"] == "unknown"
    call_id = response.json()["call_session_id"]
    again = await api.post(url, json=APPROVAL)
    assert again.status_code == 409  # never blindly re-dialled while the outcome is unknown

    status_url = f"/api/v1/simulation/calls/{call_id}/status"
    assert (await api.post(status_url, json={"status": "in-progress", "sequence": 1})).json() == {
        "result": "applied:in_progress"
    }
    assert (await api.post(status_url, json={"status": "completed", "sequence": 3})).json() == {
        "result": "applied:completed"
    }
    assert (
        (await api.post(status_url, json={"status": "ringing", "sequence": 2}))
        .json()["result"]
        .startswith("ignored")
    )  # out of order
    assert (
        (await api.post(status_url, json={"status": "completed", "sequence": 3}))
        .json()["result"]
        .startswith("ignored")
    )  # duplicate
    detail = (await api.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
    assert detail["status"] == "call_completed"
    installment = (await api.get(f"/api/v1/installments/{case['installment_id']}")).json()
    assert installment["payment_status"] == "failed"  # call outcome != payment outcome


async def test_reject_case(api: httpx.AsyncClient, mock: MockTelephonyProvider) -> None:
    await start(api)
    case = await case_for(api, "CUS-2003")
    rejected = await api.post(
        f"/api/v1/recovery/{case['recovery_case_id']}/reject",
        json={"rejected_by": "Test Operator", "reason": "Not today"},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    response = await api.post(f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL)
    assert response.status_code == 409 and mock.calls == []


# --- API contract ------------------------------------------------------------------------------


async def test_simulation_api_validation_and_dev_protection(
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    await start(api)
    both = await api.post(
        "/api/v1/simulation/advance",
        json={"simulation_id": SIM, "advance_days": 1, "target_time": "2026-10-20T00:00:00+05:30"},
    )
    assert both.status_code == 422
    neither = await api.post("/api/v1/simulation/advance", json={"simulation_id": SIM})
    assert neither.status_code == 422
    negative = await api.post(
        "/api/v1/simulation/advance", json={"simulation_id": SIM, "advance_days": -2}
    )
    assert negative.status_code == 422
    backwards = await api.post(
        "/api/v1/simulation/advance",
        json={"simulation_id": SIM, "target_time": "2026-10-02T00:00:00+05:30"},
    )
    assert backwards.status_code == 409
    missing = await api.post(
        "/api/v1/simulation/advance", json={"simulation_id": "nope", "advance_days": 1}
    )
    assert missing.status_code == 404
    days = await api.post(
        "/api/v1/simulation/advance", json={"simulation_id": SIM, "advance_days": 2}
    )
    assert days.json()["current_time"] == "2026-10-12T12:00:00+05:30"

    page = (
        await api.get(
            "/api/v1/simulation/events", params={"simulation_id": SIM, "limit": 5, "offset": 5}
        )
    ).json()
    assert len(page["items"]) == 5 and page["total"] > 10
    summary = (await api.get("/api/v1/summary", params={"simulation_id": SIM})).json()
    assert summary["calls_initiated"] == 0
    assert summary["recovery_cases_by_status"] == {"awaiting_approval": 2}
    assert len((await api.get("/api/v1/simulation/scenarios")).json()) == 6

    bad_reset = await api.post(
        "/api/v1/simulation/reset", json={"simulation_id": SIM, "confirm": "yes"}
    )
    assert bad_reset.status_code == 422
    reset = await api.post(
        "/api/v1/simulation/reset", json={"simulation_id": SIM, "confirm": f"RESET {SIM}"}
    )
    assert reset.status_code == 200 and reset.json()["generation"] == 2
    state = (await api.get("/api/v1/simulation/state", params={"simulation_id": SIM})).json()
    assert state["current_time"] == "2026-10-01T00:00:00+05:30"
    assert (
        await api.get(
            "/api/v1/simulation/state", params={"simulation_id": SIM}, headers={"X-API-Key": "x"}
        )
    ).status_code == 401


async def test_dev_endpoints_hidden_when_disabled(
    tmp_path: Path, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed_customers(session_factory, three_customers())
    settings = make_settings(tmp_path, simulation_dev_endpoints_enabled=False)
    async for http in _client(settings, MockTelephonyProvider()):
        await start(http)
        assert (
            await http.post(
                "/api/v1/simulation/reset", json={"simulation_id": SIM, "confirm": f"RESET {SIM}"}
            )
        ).status_code == 404
        installments = (
            await http.get("/api/v1/installments", params={"simulation_id": SIM})
        ).json()["items"]
        assert (
            await http.post(
                f"/api/v1/installments/{installments[0]['installment_id']}/inject-scenario",
                json={"scenario_id": "ALREADY_PAID", "reason": "test"},
            )
        ).status_code == 404


async def test_twilio_webhook_requires_configuration_and_signature(
    tmp_path: Path,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
) -> None:
    url = "/api/v1/twilio/status?call_session_id=abc"
    form = {"CallSid": "CA123", "CallStatus": "completed"}
    assert (await api.post(url, data=form)).status_code == 503  # not configured
    settings = make_settings(
        tmp_path, twilio_auth_token="test-token", public_base_url="https://example.invalid"
    )
    async for http in _client(settings, MockTelephonyProvider()):
        response = await http.post(url, data=form, headers={"X-Twilio-Signature": "forged"})
        assert response.status_code == 403
        assert (await http.post(url, data=form)).status_code == 403


async def test_health_reports_mock_telephony(api: httpx.AsyncClient) -> None:
    body = (await api.get("/api/v1/health")).json()
    assert body["providers"]["telephony"] == "mock"
    assert body["providers"]["live_calls_enabled"] is False


async def test_retried_relative_advance_with_same_key_is_replayed(api: httpx.AsyncClient) -> None:
    """Regression: `advance_days` is relative; a retry must not be recomputed from the new clock."""
    await api.post("/api/v1/simulation", json={"simulation_id": SIM, "scenario_assignments": MIX})
    body = {"simulation_id": SIM, "advance_days": 10, "idempotency_key": "retry-key-0001"}
    first = await api.post("/api/v1/simulation/advance", json=body)
    retry = await api.post("/api/v1/simulation/advance", json=body)
    assert first.status_code == retry.status_code == 200, retry.text
    assert retry.json()["replayed"] is True
    assert retry.json()["advance_id"] == first.json()["advance_id"]
    assert (
        retry.json()["current_time"] == first.json()["current_time"] == "2026-10-11T00:00:00+05:30"
    )
    state = (await api.get("/api/v1/simulation/state", params={"simulation_id": SIM})).json()
    assert state["current_time"] == "2026-10-11T00:00:00+05:30"  # not advanced twice
    conflicting = await api.post(
        "/api/v1/simulation/advance",
        json={
            "simulation_id": SIM,
            "target_time": "2026-12-01T00:00:00+05:30",
            "idempotency_key": "retry-key-0001",
        },
    )
    assert conflicting.status_code == 409


async def test_twilio_webhook_with_valid_official_signature(
    tmp_path: Path, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from twilio.request_validator import RequestValidator

    await seed_customers(session_factory, three_customers())
    settings = make_settings(
        tmp_path, twilio_auth_token="test-token", public_base_url="https://example.invalid"
    )
    async for http in _client(settings, MockTelephonyProvider()):
        await start(http)
        case = await case_for(http, "CUS-2002")
        approved = await http.post(
            f"/api/v1/recovery/{case['recovery_case_id']}/approve", json=APPROVAL
        )
        call_id = approved.json()["call_session_id"]
        provider_call_id = approved.json()["provider_call_id"]
        path = f"/api/v1/twilio/status?call_session_id={call_id}"
        form = {"CallSid": provider_call_id, "CallStatus": "completed", "SequenceNumber": "4"}
        signature = RequestValidator("test-token").compute_signature(
            f"https://example.invalid{path}", form
        )
        ok = await http.post(path, data=form, headers={"X-Twilio-Signature": signature})
        assert ok.status_code == 204
        replay = await http.post(path, data=form, headers={"X-Twilio-Signature": signature})
        assert replay.status_code == 204  # duplicate delivery is accepted and ignored
        tampered = await http.post(
            path, data=form | {"CallStatus": "busy"}, headers={"X-Twilio-Signature": signature}
        )
        assert tampered.status_code == 403
        detail = (await http.get(f"/api/v1/recovery/{case['recovery_case_id']}")).json()
        assert detail["status"] == "call_completed"
