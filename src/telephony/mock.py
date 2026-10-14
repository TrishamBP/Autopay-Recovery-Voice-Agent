"""Default telephony provider: records the call, never dials anything."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from src.telephony.base import CallInitiationResult, TelephonyError, TelephonyOutcomeUnknownError


@dataclass
class MockCall:
    recovery_case_id: str
    call_session_id: str
    destination: str
    idempotency_key: str
    provider_call_id: str


@dataclass
class MockTelephonyProvider:
    """`failure_mode` lets tests exercise provider failures without a network:
    error = definitive refusal; timeout = no call created, response lost;
    timeout_created = the call WAS created but the response was lost (ambiguous)."""

    name: str = "mock"
    failure_mode: Literal["none", "error", "timeout", "timeout_created"] = "none"
    calls: list[MockCall] = field(default_factory=list)

    async def initiate_call(
        self,
        *,
        recovery_case_id: str,
        call_session_id: str,
        destination: str,
        idempotency_key: str,
    ) -> CallInitiationResult:
        if self.failure_mode == "error":
            raise TelephonyError("mock provider rejected the call")
        if self.failure_mode == "timeout":
            raise TelephonyOutcomeUnknownError("mock provider timed out")
        existing = next((c for c in self.calls if c.idempotency_key == idempotency_key), None)
        if existing is None:
            existing = MockCall(
                recovery_case_id,
                call_session_id,
                destination,
                idempotency_key,
                f"MOCK-{uuid.uuid4().hex[:24]}",
            )
            self.calls.append(existing)
        if self.failure_mode == "timeout_created":
            raise TelephonyOutcomeUnknownError("mock provider timed out after creating the call")
        return CallInitiationResult(self.name, existing.provider_call_id, "queued")

    async def find_call(
        self, *, call_session_id: str, destination: str, created_after: datetime
    ) -> str | None:
        match = next((c for c in self.calls if c.call_session_id == call_session_id), None)
        return match.provider_call_id if match else None
