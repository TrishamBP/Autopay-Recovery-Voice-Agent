"""Selects the telephony provider. Mock unless Twilio is explicitly and fully enabled."""

from src.config import Settings
from src.telephony.base import TelephonyProvider
from src.telephony.mock import MockTelephonyProvider


def create_provider(settings: Settings) -> TelephonyProvider:
    if settings.telephony_provider == "mock":
        return MockTelephonyProvider()
    if not settings.twilio_ready:
        raise ValueError(
            "TELEPHONY_PROVIDER=twilio needs TWILIO_LIVE_CALLS_ENABLED=true plus credentials, "
            "TWILIO_FROM_NUMBER and PUBLIC_BASE_URL. Use TELEPHONY_PROVIDER=mock otherwise."
        )
    from src.telephony.twilio import TwilioTelephonyProvider

    return TwilioTelephonyProvider(settings)
