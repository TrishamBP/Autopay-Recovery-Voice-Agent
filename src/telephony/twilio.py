"""Twilio Programmable Voice adapter (live calls; disabled unless explicitly enabled).

The Twilio Python SDK is synchronous, so requests run in a worker thread with an explicit HTTP
timeout. Twilio's Calls API has no idempotency key, so duplicates are prevented locally (atomic
approval claim + one active call per case). A read timeout is reported as an UNKNOWN outcome and
reconciled (status callback, voice webhook, or `find_call`) before any new attempt.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from src.common.redaction import redact_transcript
from src.config import Settings
from src.telephony.base import CallInitiationResult, TelephonyError, TelephonyOutcomeUnknownError
from src.voice.twiml import STATUS_PATH, VOICE_PATH, webhook_url

STATUS_EVENTS = ["initiated", "ringing", "answered", "completed"]


class TwilioTelephonyProvider:
    name = "twilio"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        if not settings.twilio_ready or settings.twilio_auth_token is None:
            raise ValueError(
                "Twilio requires TWILIO_ENABLED=true, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                "TWILIO_FROM_NUMBER and an https:// PUBLIC_BASE_URL"
            )
        self._settings = settings
        if client is None:
            from twilio.http.http_client import TwilioHttpClient
            from twilio.rest import Client

            client = Client(
                settings.twilio_account_sid,
                settings.twilio_auth_token.get_secret_value(),
                http_client=TwilioHttpClient(timeout=settings.telephony_timeout_seconds),
            )
        self._client: Any = client

    def _create(self, destination: str, call_session_id: str) -> Any:
        s = self._settings
        required = {
            "to": destination,
            "from_": s.twilio_from_number,
            "url": webhook_url(s, VOICE_PATH, call_session_id=call_session_id),  # POSTed
        }
        if s.twilio_trial_account:
            # Trial accounts reject every optional parameter ("limited parameter access"), so
            # only To/From/Url are sent. There are no status callbacks: sync the final status
            # with `scripts.manage sync-call-status`. The conversation engine still enforces
            # VOICE_MAX_CALL_SECONDS and the turn limit.
            return self._client.calls.create(**required)
        return self._client.calls.create(
            **required,
            method="POST",
            status_callback=webhook_url(s, STATUS_PATH, call_session_id=call_session_id),
            status_callback_event=STATUS_EVENTS,
            status_callback_method="POST",
            timeout=30,  # seconds to ring before no-answer
            time_limit=s.voice_max_call_seconds,  # hard cap on call duration
        )

    async def initiate_call(
        self,
        *,
        recovery_case_id: str,
        call_session_id: str,
        destination: str,
        idempotency_key: str,
    ) -> CallInitiationResult:
        from requests.exceptions import ConnectionError as ConnectError
        from requests.exceptions import ConnectTimeout, ReadTimeout
        from twilio.base.exceptions import TwilioRestException

        try:
            call = await asyncio.to_thread(self._create, destination, call_session_id)
        except TwilioRestException as exc:
            # Twilio's message explains the refusal; numbers are redacted before it is stored.
            detail = redact_transcript(str(getattr(exc, "msg", "") or ""))[:160]
            raise TelephonyError(
                f"Twilio rejected the call (HTTP {exc.status}, error {exc.code}): {detail}"
            ) from exc
        except ConnectTimeout as exc:  # request never reached Twilio
            raise TelephonyError("Twilio connection timed out") from exc
        except ReadTimeout as exc:  # sent, response lost: a call MAY exist
            raise TelephonyOutcomeUnknownError("Twilio response timed out") from exc
        except ConnectError as exc:
            raise TelephonyError("Twilio unreachable") from exc
        return CallInitiationResult(self.name, str(call.sid), "queued")

    async def find_call(
        self, *, call_session_id: str, destination: str, created_after: datetime
    ) -> str | None:
        """Look up a call this app may have created (for ambiguous initiations)."""
        calls = await asyncio.to_thread(
            lambda: self._client.calls.list(
                to=destination,
                from_=self._settings.twilio_from_number,
                start_time_after=created_after - timedelta(minutes=1),
                limit=5,
            )
        )
        return str(calls[0].sid) if calls else None


def validate_twilio_signature(
    settings: Settings, url: str, params: dict[str, str], signature: str
) -> bool:
    if settings.twilio_auth_token is None:
        return False
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    return bool(validator.validate(url, params, signature))
