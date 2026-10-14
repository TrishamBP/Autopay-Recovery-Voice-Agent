"""Typed application settings, read from environment variables (and `.env` if present)."""

import re
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/autopay_recovery.db"
DEFAULT_WORKBOOK_PATH = Path("../synthetic-data/data/autopay_recovery_customers.xlsx")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_docs_enabled: bool = True  # set false when the app is reachable through a public tunnel

    # Database
    database_url: str = DEFAULT_DATABASE_URL
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=0, le=60_000)
    sqlite_wal: bool = True
    database_echo: bool = False

    # Operator API (protects every /api/v1 route except /health)
    operator_api_key: SecretStr | None = None

    # Fireworks AI (OpenAI-compatible chat completions API)
    fireworks_api_key: SecretStr | None = None
    fireworks_base_url: str = "https://api.fireworks.ai/inference/v1"
    fireworks_model: str | None = None
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_max_tokens: int = Field(default=600, ge=16, le=4096)
    llm_max_retries: int = Field(default=1, ge=0, le=3)

    # Natural-language read-only SQL endpoint (development/test only)
    sql_query_enabled: bool = False
    sql_max_rows: int = Field(default=100, ge=1, le=1000)
    sql_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    sql_max_question_chars: int = Field(default=500, ge=10, le=4000)
    sql_max_query_chars: int = Field(default=4000, ge=100, le=20_000)
    sql_explain_max_rows: int = Field(default=30, ge=0, le=200)

    # Business rules for typed tools
    customer_default_timezone: str = "Asia/Kolkata"
    calling_hours_start: int = Field(default=9, ge=0, le=23)
    calling_hours_end: int = Field(default=20, ge=1, le=24)
    callback_min_lead_minutes: int = Field(default=15, ge=0)
    callback_max_horizon_days: int = Field(default=14, ge=1)
    payment_link_ttl_hours: int = Field(default=24, ge=1, le=168)
    simulated_payment_base_url: str = "https://payments.example.invalid/simulated"

    # Offline import
    workbook_path: Path = DEFAULT_WORKBOOK_PATH

    # Virtual-time simulation
    simulation_default_timezone: str = "Asia/Kolkata"
    simulation_initial_time: datetime = datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    simulation_autopay_time: time = time(11, 0)  # local time of the daily autopay batch
    simulation_dev_endpoints_enabled: bool = False  # reset / inject / retry / mock call status
    simulation_max_event_attempts: int = Field(default=3, ge=1, le=10)
    simulation_max_events_per_advance: int = Field(default=20_000, ge=10)
    simulation_max_advance_days: int = Field(default=800, ge=1)

    # Recovery calls: manual approval is ALWAYS required.
    # Comma-separated E.164 numbers the tester controls. TWILIO_ALLOWED_TEST_NUMBERS is the
    # preferred variable name; TEST_DESTINATION_ALLOWLIST is still accepted.
    test_destination_allowlist: str = Field(
        default="",
        validation_alias=AliasChoices("TWILIO_ALLOWED_TEST_NUMBERS", "TEST_DESTINATION_ALLOWLIST"),
    )
    allow_synthetic_consent_for_test_destinations: bool = False

    # Telephony. Live calls need BOTH TELEPHONY_PROVIDER=twilio and TWILIO_ENABLED=true.
    telephony_provider: Literal["mock", "twilio"] = "mock"
    twilio_live_calls_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("TWILIO_ENABLED", "TWILIO_LIVE_CALLS_ENABLED")
    )
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str | None = None
    public_base_url: str | None = None
    telephony_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    telephony_reconcile_grace_seconds: int = Field(default=120, ge=0)
    # Twilio trial accounts reject optional call parameters (timeout, time_limit).
    twilio_trial_account: bool = False
    # Trial sandbox numbers send UNSIGNED webhooks. Opt-in, development/test only; see
    # src/telephony/router.py for the strict session/CallSid match that replaces the HMAC.
    twilio_trial_accept_unsigned_webhooks: bool = False
    # Webhooks without a signature are accepted ONLY when no auth token is configured, Twilio is
    # not live and APP_ENV is development/test (local curl testing of the mock flow).
    twilio_allow_unsigned_webhooks: bool = False

    # Turn-based voice conversation (Twilio <Gather input="speech"> + <Say>)
    # en-IN is documented for speechModel experimental_conversations; with a speechModel set,
    # Twilio requires an integer speechTimeout (not "auto"). Polly.Raveena is an en-IN voice.
    twilio_speech_locale: str = "en-IN"
    twilio_speech_model: str = "experimental_conversations"
    twilio_speech_timeout_seconds: int = Field(default=2, ge=1, le=10)
    twilio_gather_timeout_seconds: int = Field(default=5, ge=1, le=30)
    twilio_tts_voice: str = "Polly.Raveena"
    voice_business_name: str = Field(
        default="the subscription service",
        validation_alias=AliasChoices("VOICE_COMPANY_NAME", "VOICE_BUSINESS_NAME"),
    )
    voice_agent_name: str = "Alex"
    voice_max_unclear: int = Field(default=1, ge=0, le=3)  # clarifying questions before ending

    # Transcripts (text only; audio recording is NOT enabled). Whether retention needs consent
    # is a product/legal decision: the default discloses it in the introduction.
    transcript_policy: Literal["store_sanitized", "metadata_only"] = "store_sanitized"
    transcript_store_original: bool = False  # secrets are stripped even when true
    transcript_retention_days: int = Field(default=30, ge=1, le=3650)
    transcript_disclosure: str = (
        "This call is with an automated assistant, and a text transcript is kept for quality "
        "and record-keeping."
    )
    voice_max_turns: int = Field(default=12, ge=1, le=50)
    voice_max_silence_prompts: int = Field(default=2, ge=0, le=5)
    voice_max_call_seconds: int = Field(default=300, ge=30, le=3600)
    # Twilio waits ~15 s for a webhook response; keep the model budget well below that.
    voice_llm_timeout_seconds: float = Field(default=8.0, gt=0, le=12)
    # Twilio (notably the trial sandbox, ~5 s) drops slow webhooks; reply with a hold/redirect
    # after this long and serve the finished turn on the redirected request.
    voice_webhook_reply_within_seconds: float = Field(default=3.0, gt=0, le=10)
    voice_llm_max_tokens: int = Field(default=1200, ge=64, le=4096)
    voice_history_turns: int = Field(default=10, ge=0, le=40)

    @field_validator("simulation_initial_time")
    @classmethod
    def _aware_initial_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("SIMULATION_INITIAL_TIME must include a UTC offset")
        return value

    @property
    def allowed_test_destinations(self) -> frozenset[str]:
        numbers = {n.strip() for n in self.test_destination_allowlist.split(",") if n.strip()}
        invalid = [n for n in numbers if not _E164.match(n)]
        if invalid:
            raise ValueError("TWILIO_ALLOWED_TEST_NUMBERS must contain E.164 numbers only")
        return frozenset(numbers)

    @property
    def dev_endpoints_allowed(self) -> bool:
        return self.simulation_dev_endpoints_enabled and self.app_env in ("development", "test")

    @property
    def twilio_ready(self) -> bool:
        return bool(
            self.twilio_live_calls_enabled
            and self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_auth_token.get_secret_value()
            and self.twilio_from_number
            and self.public_base_url
            and self.public_base_url.startswith("https://")
        )

    @property
    def twilio_live(self) -> bool:
        return self.telephony_provider == "twilio" and self.twilio_ready

    def public_url(self, path: str) -> str:
        """Absolute URL Twilio uses to reach us (never derived from request headers)."""
        if not self.public_base_url:
            raise ValueError("PUBLIC_BASE_URL is not configured")
        return f"{self.public_base_url.rstrip('/')}{path}"

    @property
    def sql_query_allowed(self) -> bool:
        """The SQL endpoint exists only when explicitly enabled outside production."""
        return self.sql_query_enabled and self.app_env in ("development", "test")

    @property
    def llm_configured(self) -> bool:
        return bool(self.fireworks_api_key and self.fireworks_api_key.get_secret_value()) and bool(
            self.fireworks_model
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
