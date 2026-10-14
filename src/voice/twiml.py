"""TwiML builders (official Twilio SDK). Every action URL is absolute, from PUBLIC_BASE_URL.

Speech: <Gather input="speech"> with the configured locale/model. With a speechModel set,
Twilio requires an integer speechTimeout. actionOnEmptyResult=true makes Twilio call the
action URL on silence too, so the backend decides how to reprompt or end. <Say> is nested in
<Gather> so the caller can interrupt the prompt (barge-in).
"""

from urllib.parse import urlencode

from twilio.twiml.voice_response import Gather, VoiceResponse

from src.config import Settings

PROCESS_SPEECH_PATH = "/api/v1/twilio/process-speech"
VOICE_PATH = "/api/v1/twilio/voice"
STATUS_PATH = "/api/v1/twilio/status"


def webhook_url(settings: Settings, path: str, **query: object) -> str:
    return settings.public_url(path) + (f"?{urlencode(query)}" if query else "")


def _say(target: VoiceResponse | Gather, text: str, settings: Settings) -> None:
    target.say(text, voice=settings.twilio_tts_voice, language=settings.twilio_speech_locale)


def gather_response(settings: Settings, text: str, *, call_session_id: str, turn: int) -> str:
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=webhook_url(
            settings, PROCESS_SPEECH_PATH, call_session_id=call_session_id, turn=turn
        ),
        method="POST",
        language=settings.twilio_speech_locale,
        speech_model=settings.twilio_speech_model,
        speech_timeout=settings.twilio_speech_timeout_seconds,
        timeout=settings.twilio_gather_timeout_seconds,
        action_on_empty_result=True,
        profanity_filter=True,
    )
    _say(gather, text, settings)
    response.append(gather)
    # Only reached if Twilio cannot run the Gather action; end politely instead of looping.
    _say(response, "Sorry, we could not continue this call. Goodbye.", settings)
    response.hangup()
    return str(response)


def hangup_response(settings: Settings, text: str) -> str:
    response = VoiceResponse()
    _say(response, text, settings)
    response.hangup()
    return str(response)


def reject_response() -> str:
    """For calls that fail verification: no account details, no prompts, just hang up."""
    response = VoiceResponse()
    response.hangup()
    return str(response)


def hold_response(settings: Settings, *, call_session_id: str, turn: int) -> str:
    """A duplicate delivery arrived while the first is still being processed: pause, then
    re-request the same turn (which replays the stored reply once it exists)."""
    response = VoiceResponse()
    response.pause(length=1)
    response.redirect(
        webhook_url(settings, PROCESS_SPEECH_PATH, call_session_id=call_session_id, turn=turn),
        method="POST",
    )
    return str(response)
