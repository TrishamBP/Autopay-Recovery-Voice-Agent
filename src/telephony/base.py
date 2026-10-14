"""Provider-independent telephony interface."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class CallInitiationResult:
    provider: str
    provider_call_id: str
    status: Literal["queued", "in_progress"]


class TelephonyError(Exception):
    """The provider definitively did NOT create a call (safe to record as failed)."""


class TelephonyOutcomeUnknownError(Exception):
    """The request may or may not have created a call (e.g. read timeout). Never retry blindly:
    reconcile through the provider's status callback first."""


class TelephonyProvider(Protocol):
    name: str

    async def initiate_call(
        self,
        *,
        recovery_case_id: str,
        call_session_id: str,
        destination: str,
        idempotency_key: str,
    ) -> CallInitiationResult: ...

    async def find_call(
        self, *, call_session_id: str, destination: str, created_after: datetime
    ) -> str | None:
        """Provider call id of a call this app created, or None if none exists."""
        ...
