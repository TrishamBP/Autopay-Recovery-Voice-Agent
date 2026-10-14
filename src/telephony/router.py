"""Twilio webhooks (`/api/v1/twilio/*`). Authenticated by `X-Twilio-Signature`, validated with the
official SDK over the externally visible URL = PUBLIC_BASE_URL + the request path + query.
Forwarded headers (X-Forwarded-Host/Proto) are deliberately ignored: they are client-controlled.

Unsigned requests are accepted only when no auth token is configured, Twilio is not live,
APP_ENV is development/test and TWILIO_ALLOW_UNSIGNED_WEBHOOKS=true (local mock testing).

Trial exception (TWILIO_TRIAL_ACCEPT_UNSIGNED_WEBHOOKS): Twilio's trial *sandbox* number forwards
webhooks WITHOUT a signature. Only for requests that carry no signature at all, and only when
trial + live + development/test, a request is accepted if its AccountSid is ours AND the
call_session_id exists with an active call AND its CallSid exactly equals the SID Twilio returned
to our own Calls API request. Weaker than HMAC (it relies on those identifiers staying private);
each acceptance is logged as a warning. A request that carries a signature must still validate.
"""

import logging
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, Query, Request, Response

from src.calls.models import CallSession
from src.common.dependencies import SettingsDep
from src.common.enums import CallStatus
from src.common.exceptions import AppError, ServiceUnavailableError
from src.config import Settings
from src.telephony.status import apply_call_status
from src.telephony.twilio import validate_twilio_signature
from src.voice.conversation import TwilioParams, VoiceConversationService
from src.voice.twiml import PROCESS_SPEECH_PATH, STATUS_PATH, VOICE_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])
MAX_BODY_BYTES = 16_384
TWIML = "application/xml"
CallSessionId = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")]


class InvalidSignature(AppError):
    status_code = 403
    code = "invalid_signature"


def _unsigned_allowed(settings: Settings) -> bool:
    return (
        settings.twilio_allow_unsigned_webhooks
        and settings.twilio_auth_token is None
        and not settings.twilio_live
        and settings.app_env in ("development", "test")
    )


def trial_unsigned_allowed(settings: Settings) -> bool:
    return (
        settings.twilio_trial_accept_unsigned_webhooks
        and settings.twilio_trial_account
        and settings.twilio_live
        and settings.app_env in ("development", "test")
    )


async def _trial_session_matches(
    request: Request, settings: Settings, call_session_id: str, params: dict[str, str]
) -> bool:
    if params.get("AccountSid") != settings.twilio_account_sid or not params.get("CallSid"):
        return False
    async with request.app.state.session_factory() as session:
        call = await session.get(CallSession, call_session_id)
        return bool(
            call is not None
            and call.provider == "twilio"
            and call.provider_call_id is not None
            and call.provider_call_id == params["CallSid"]
            and call.status in (CallStatus.QUEUED, CallStatus.IN_PROGRESS)
        )


async def verified_form(
    request: Request, settings: Settings, signature: str | None, call_session_id: str
) -> dict[str, str]:
    """Parse the form body (urlencoded; no multipart dependency) and verify the signature."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise InvalidSignature("Invalid Twilio request")
    params = dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    if _unsigned_allowed(settings):
        return params
    if settings.twilio_auth_token is None or not settings.public_base_url:
        raise ServiceUnavailableError("Twilio webhooks are not configured")
    query = request.url.query
    url = settings.public_url(request.url.path) + (f"?{query}" if query else "")
    if (
        not signature
        and trial_unsigned_allowed(settings)
        and await _trial_session_matches(request, settings, call_session_id, params)
    ):
        logger.warning(
            "unsigned trial webhook accepted (session and CallSid matched)",
            extra={"path": request.url.path, "call_session_id": call_session_id},
        )
        return params
    if not signature or not validate_twilio_signature(settings, url, params, signature):
        logger.warning(
            "twilio signature rejected",
            extra=_rejection_diagnostics(settings, request, url, params, signature),
        )
        raise InvalidSignature("Invalid Twilio signature")
    return params


def _rejection_diagnostics(
    settings: Settings, request: Request, url: str, params: dict[str, str], signature: str | None
) -> dict[str, object]:
    """Non-secret facts for diagnosing a rejected signature. The request is still rejected;
    this only reports which URL variant (if any) the signature would have matched."""
    base = (settings.public_base_url or "").rstrip("/")
    path, query = request.url.path, request.url.query
    variants = {
        "port_443": base + ":443" + path + (f"?{query}" if query else ""),
        "no_query": base + path,
        "http_scheme": base.replace("https://", "http://", 1)
        + path
        + (f"?{query}" if query else ""),
    }
    matches = [
        name
        for name, candidate in variants.items()
        if signature and validate_twilio_signature(settings, candidate, params, signature)
    ]
    return {
        "path": path,
        "signature_present": bool(signature),
        "account_sid_matches": params.get("AccountSid") == settings.twilio_account_sid,
        "param_names": sorted(params),
        "matching_url_variants": matches,
        "forwarded_host": request.headers.get("x-forwarded-host"),
    }


def _voice(request: Request) -> VoiceConversationService:
    return request.app.state.voice


@router.post(VOICE_PATH.removeprefix("/api/v1/twilio"), response_class=Response)
async def voice(
    request: Request,
    settings: SettingsDep,
    call_session_id: CallSessionId,
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """The callee answered: greet, disclose automation, and start collecting speech."""
    params = await verified_form(request, settings, x_twilio_signature, call_session_id)
    xml = await _voice(request).start(call_session_id, TwilioParams.from_form(params))
    return Response(content=xml, media_type=TWIML)


@router.post(PROCESS_SPEECH_PATH.removeprefix("/api/v1/twilio"), response_class=Response)
async def process_speech(
    request: Request,
    settings: SettingsDep,
    call_session_id: CallSessionId,
    turn: Annotated[int, Query(ge=1, le=100)],
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """One caller utterance (SpeechResult; empty on silence) -> one spoken agent reply."""
    params = await verified_form(request, settings, x_twilio_signature, call_session_id)
    xml = await _voice(request).handle_speech(call_session_id, turn, TwilioParams.from_form(params))
    return Response(content=xml, media_type=TWIML)


@router.post(STATUS_PATH.removeprefix("/api/v1/twilio"), status_code=204)
async def status(
    request: Request,
    settings: SettingsDep,
    call_session_id: CallSessionId,
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """Call lifecycle callbacks. Duplicate / out-of-order deliveries are ignored safely."""
    params = await verified_form(request, settings, x_twilio_signature, call_session_id)
    sequence, duration = params.get("SequenceNumber"), params.get("CallDuration")
    result = await apply_call_status(
        request.app.state.session_factory,
        call_session_id=call_session_id,
        provider_status=params.get("CallStatus", ""),
        provider_call_id=params.get("CallSid"),
        sequence=int(sequence) if sequence and sequence.isdigit() else None,
        duration_seconds=int(duration) if duration and duration.isdigit() else None,
    )
    logger.info("twilio status", extra={"call_session_id": call_session_id, "result": result})
    return Response(status_code=204)


@router.get("/health")
async def twilio_health(request: Request, settings: SettingsDep) -> dict[str, object]:
    """Non-sensitive integration status. Never returns credentials or the account SID."""
    https = bool(settings.public_base_url and settings.public_base_url.startswith("https://"))
    return {
        "telephony_provider": request.app.state.telephony.name,
        "twilio_enabled": settings.twilio_live_calls_enabled,
        "live_calls_possible": settings.twilio_live,
        "credentials_configured": bool(settings.twilio_account_sid and settings.twilio_auth_token),
        "from_number_configured": bool(settings.twilio_from_number),
        "public_base_url_https": https,
        "webhook_urls": {
            "voice": settings.public_url(VOICE_PATH) if https else None,
            "status": settings.public_url(STATUS_PATH) if https else None,
        },
        "allowed_test_numbers": len(settings.allowed_test_destinations),
        "speech": {
            "locale": settings.twilio_speech_locale,
            "model": settings.twilio_speech_model,
            "voice": settings.twilio_tts_voice,
        },
        "llm_configured": settings.llm_configured,
        "unsigned_webhooks_allowed": _unsigned_allowed(settings),
        "trial_unsigned_webhooks_accepted": trial_unsigned_allowed(settings),
    }
