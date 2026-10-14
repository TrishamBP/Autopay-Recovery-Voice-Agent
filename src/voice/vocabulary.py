"""Conversation vocabulary (pure enums; imported by models and the voice engine).

`ConversationIntent` is the spec's intent list plus two stage-confirmation intents used during
the opening (IDENTITY_CONFIRMED, AGREE_TO_CONTINUE).
"""

from enum import StrEnum


class ConversationStage(StrEnum):
    IDENTITY_CHECK = "identity_check"  # "Hello, may I speak with {name}?"
    CONVENIENCE_CHECK = "convenience_check"  # Alex introduction + "convenient time?"
    DISCUSSION = "discussion"  # verified facts disclosed; resolving
    CALLBACK_TIME = "callback_time"  # busy before disclosure: asking only for a callback time
    ENDED = "ended"


class ConversationIntent(StrEnum):
    IDENTITY_CONFIRMED = "IDENTITY_CONFIRMED"
    AGREE_TO_CONTINUE = "AGREE_TO_CONTINUE"
    RETRY_PAYMENT = "RETRY_PAYMENT"
    REQUEST_PAYMENT_LINK = "REQUEST_PAYMENT_LINK"
    CHECK_WITH_BANK = "CHECK_WITH_BANK"
    ALREADY_PAID = "ALREADY_PAID"
    DISPUTE_PAYMENT = "DISPUTE_PAYMENT"
    REQUEST_CALLBACK = "REQUEST_CALLBACK"
    REQUEST_HUMAN_SUPPORT = "REQUEST_HUMAN_SUPPORT"
    ASK_QUESTION = "ASK_QUESTION"
    DECLINE_ASSISTANCE = "DECLINE_ASSISTANCE"
    DO_NOT_CALL = "DO_NOT_CALL"
    IDENTITY_NOT_CONFIRMED = "IDENTITY_NOT_CONFIRMED"
    UNCLEAR_RESPONSE = "UNCLEAR_RESPONSE"
    OTHER = "OTHER"


class ResolutionPreference(StrEnum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    PAYMENT_LINK = "PAYMENT_LINK"
    CHECK_WITH_BANK = "CHECK_WITH_BANK"
    CALLBACK = "CALLBACK"
    HUMAN_SUPPORT = "HUMAN_SUPPORT"


class BackendAction(StrEnum):
    """Allowlist. The model may PROPOSE one; the backend policy decides whether it runs."""

    CHECK_RETRY_ELIGIBILITY = "check_retry_eligibility"
    REQUEST_PAYMENT_LINK = "request_payment_link"
    RECORD_PAYMENT_CLAIM = "record_payment_claim"
    RECORD_DISPUTE = "record_dispute"
    SCHEDULE_CALLBACK = "schedule_callback"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    RECORD_DO_NOT_CALL = "record_do_not_call"


class TurnSpeaker(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class TurnStatus(StrEnum):
    RECEIVED = "received"  # customer turn stored, processing not finished
    PROCESSED = "processed"
    FALLBACK = "fallback"  # model unavailable/invalid; a safe fallback was used
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    """Agent turns: `returned` = TwiML handed to Twilio (not proof it was heard);
    `played` = Twilio's next callback for this call implies the prompt ran."""

    RETURNED = "returned"
    PLAYED = "played"


class ActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    NOT_AVAILABLE = "not_available"


class TranscriptConsent(StrEnum):
    DISCLOSED = "disclosed"  # transcript notice given in the introduction
    NOT_RETAINED = "not_retained"  # TRANSCRIPT_POLICY=metadata_only


class CallEventType(StrEnum):
    CALL_APPROVED = "CALL_APPROVED"
    CALL_INITIATION_REQUESTED = "CALL_INITIATION_REQUESTED"
    CALL_INITIATED = "CALL_INITIATED"
    CALL_INITIATION_UNKNOWN = "CALL_INITIATION_UNKNOWN"
    CALL_RINGING = "CALL_RINGING"
    CALL_ANSWERED = "CALL_ANSWERED"
    CALL_COMPLETED = "CALL_COMPLETED"
    CALL_NO_ANSWER = "CALL_NO_ANSWER"
    CALL_BUSY = "CALL_BUSY"
    CALL_FAILED = "CALL_FAILED"
    CONVERSATION_STARTED = "CONVERSATION_STARTED"
    CONVERSATION_TURN_RECEIVED = "CONVERSATION_TURN_RECEIVED"
    AGENT_RESPONSE_GENERATED = "AGENT_RESPONSE_GENERATED"
    LLM_FALLBACK_USED = "LLM_FALLBACK_USED"
    BACKEND_ACTION_REQUESTED = "BACKEND_ACTION_REQUESTED"
    BACKEND_ACTION_SUCCEEDED = "BACKEND_ACTION_SUCCEEDED"
    BACKEND_ACTION_FAILED = "BACKEND_ACTION_FAILED"
    CUSTOMER_REPORTED_ALREADY_PAID = "CUSTOMER_REPORTED_ALREADY_PAID"
    CUSTOMER_REQUESTED_CALLBACK = "CUSTOMER_REQUESTED_CALLBACK"
    CUSTOMER_DISPUTED_PAYMENT = "CUSTOMER_DISPUTED_PAYMENT"
    CUSTOMER_REQUESTED_NO_FURTHER_CALLS = "CUSTOMER_REQUESTED_NO_FURTHER_CALLS"
    CONVERSATION_ENDED = "CONVERSATION_ENDED"


class EventSource(StrEnum):
    TWILIO = "TWILIO"
    BACKEND = "BACKEND"
    LLM = "LLM"
    SYSTEM = "SYSTEM"
