"""Call-related tables. Nothing here places calls: a `call_session` row for a recovery call is
created only by the manual-approval service, after every policy check passes."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.common.enums import CallbackStatus, CallOutcome, CallStatus, TurnRole
from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base
from src.voice.vocabulary import (
    CallEventType,
    ConversationIntent,
    ConversationStage,
    DeliveryStatus,
    EventSource,
    ResolutionPreference,
    TranscriptConsent,
    TurnStatus,
)


class CallSession(Base):
    __tablename__ = "call_session"
    __table_args__ = (
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_nonnegative"
        ),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at",
            name="ended_after_started",
        ),
        # At most one active (queued / in progress) call per recovery case.
        Index(
            "uq_call_session_active_recovery_case",
            "recovery_case_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'in_progress')"),
            postgresql_where=text("status IN ('queued', 'in_progress')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), unique=True)
    recovery_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_case.recovery_case_id", ondelete="SET NULL"), index=True
    )
    simulation_id: Mapped[str | None] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str | None] = mapped_column(String(16))  # mock | twilio
    provider_call_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    destination_masked: Mapped[str | None] = mapped_column(String(24))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    last_status_sequence: Mapped[int | None] = mapped_column(Integer)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[CallStatus] = mapped_column(enum_column(CallStatus, "status"), index=True)
    recovery_outcome: Mapped[CallOutcome | None] = mapped_column(
        enum_column(CallOutcome, "recovery_outcome")
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    termination_reason: Mapped[str | None] = mapped_column(String(200))
    conversation_stage: Mapped[ConversationStage | None] = mapped_column(
        enum_column(ConversationStage, "conversation_stage")
    )
    transcript_consent: Mapped[TranscriptConsent | None] = mapped_column(
        enum_column(TranscriptConsent, "transcript_consent")
    )
    transcript_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class ConversationTurn(Base):
    """One utterance. Customer turn N = sequence 2N-1, agent reply = 2N, greeting = 0.

    `transcript_text` is the sanitized (redacted) text used for audit and display.
    `original_text` is kept only when TRANSCRIPT_STORE_ORIGINAL=true, and even then secrets
    (OTPs, PINs, card numbers, passwords) are removed before it is written.
    """

    __tablename__ = "conversation_turn"
    __table_args__ = (
        UniqueConstraint("call_session_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_session_id: Mapped[str] = mapped_column(
        ForeignKey("call_session.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[TurnRole] = mapped_column(enum_column(TurnRole, "role"))
    transcript_text: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    provider_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON)
    # Audit fields (all nullable: rows written before this migration stay valid).
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    turn_number: Mapped[int | None] = mapped_column(Integer)
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    recovery_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_case.recovery_case_id", ondelete="SET NULL"), index=True
    )
    original_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    virtual_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    intent: Mapped[ConversationIntent | None] = mapped_column(
        enum_column(ConversationIntent, "intent")
    )
    resolution_preference: Mapped[ResolutionPreference | None] = mapped_column(
        enum_column(ResolutionPreference, "resolution_preference")
    )
    backend_action: Mapped[str | None] = mapped_column(String(48))
    action_status: Mapped[str | None] = mapped_column(String(16))
    action_result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    processing_status: Mapped[TurnStatus | None] = mapped_column(
        enum_column(TurnStatus, "processing_status")
    )
    error_category: Mapped[str | None] = mapped_column(String(64))
    delivery_status: Mapped[DeliveryStatus | None] = mapped_column(
        enum_column(DeliveryStatus, "delivery_status")
    )


class CallEvent(Base):
    """Append-only call lifecycle / conversation event. `idempotency_key` deduplicates repeated
    deliveries (e.g. Twilio retries). `details` holds non-sensitive metadata only."""

    __tablename__ = "call_event"
    __table_args__ = (
        Index("ix_call_event_call_session_occurred", "call_session_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    call_session_id: Mapped[str] = mapped_column(ForeignKey("call_session.id", ondelete="CASCADE"))
    recovery_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("recovery_case.recovery_case_id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    turn_number: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[CallEventType] = mapped_column(
        enum_column(CallEventType, "event_type"), index=True
    )
    source: Mapped[EventSource] = mapped_column(enum_column(EventSource, "source"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    virtual_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    details: Mapped[dict[str, object] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)


class CallbackRequest(Base):
    __tablename__ = "callback_request"
    __table_args__ = (Index("ix_callback_request_status_requested_at", "status", "requested_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    call_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_session.id", ondelete="SET NULL"), index=True
    )
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    timezone: Mapped[str] = mapped_column(String(64))
    status: Mapped[CallbackStatus] = mapped_column(enum_column(CallbackStatus, "status"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
