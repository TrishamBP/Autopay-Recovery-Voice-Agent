"""Shared domain vocabulary.

Values are copied verbatim from the workbook's `Enums` sheet / DATA_DICTIONARY.md. The importer
verifies at import time that `WORKBOOK_ENUMS` matches the sheet exactly, so drift is caught.
The enums below the divider are backend-only (not part of the workbook).
"""

from enum import StrEnum


class Currency(StrEnum):
    INR = "INR"


class BillingFrequency(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class PaymentStatus(StrEnum):
    """State of the customer's current charge. `paid` only via a trusted confirmation event."""

    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    PENDING_CONFIRMATION = "pending_confirmation"
    PAID = "paid"
    CANCELLED = "cancelled"


class PaymentHistoryStatus(StrEnum):
    """Final outcome of one billing cycle (not of an individual attempt)."""

    PAID_ON_TIME = "paid_on_time"
    PAID_LATE = "paid_late"
    FAILED = "failed"
    MISSED = "missed"
    PARTIALLY_PAID = "partially_paid"


FULLY_PAID_STATUSES = frozenset({PaymentHistoryStatus.PAID_ON_TIME, PaymentHistoryStatus.PAID_LATE})
UNPAID_STATUSES = frozenset({PaymentHistoryStatus.FAILED, PaymentHistoryStatus.MISSED})


class PaymentFailureReason(StrEnum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_CARD = "expired_card"
    BANK_DECLINED = "bank_declined"
    MANDATE_INACTIVE = "mandate_inactive"
    TECHNICAL_ERROR = "technical_error"


class PaymentMethodType(StrEnum):
    CARD = "card"
    UPI = "UPI"
    NET_BANKING = "net_banking"
    E_MANDATE = "e-mandate"


class RecoveryStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    CALLBACK_SCHEDULED = "callback_scheduled"
    LINK_SENT = "link_sent"
    UNDER_REVIEW = "under_review"
    RECOVERED = "recovered"
    OPTED_OUT = "opted_out"
    CLOSED_UNRECOVERED = "closed_unrecovered"


class CallStatus(StrEnum):
    NOT_CALLED = "not_called"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    FAILED = "failed"


class CallOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    PAYMENT_LINK_SENT = "payment_link_sent"
    PAYMENT_METHOD_UPDATE_LINK_SENT = "payment_method_update_link_sent"
    CALLBACK_SCHEDULED = "callback_scheduled"
    PAYMENT_CLAIM_LOGGED = "payment_claim_logged"
    FAILURE_EXPLAINED = "failure_explained"
    DO_NOT_CONTACT_REQUESTED = "do_not_contact_requested"
    CUSTOMER_DECLINED = "customer_declined"
    ESCALATED_TO_HUMAN = "escalated_to_human"
    WRONG_PERSON = "wrong_person"
    NO_ANSWER = "no_answer"


class PaymentLinkStatus(StrEnum):
    NOT_CREATED = "not_created"
    CREATED = "created"
    SENT = "sent"
    OPENED = "opened"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SupportedLanguage(StrEnum):
    ENGLISH = "English"
    HINDI = "Hindi"
    TAMIL = "Tamil"
    TELUGU = "Telugu"
    MARATHI = "Marathi"
    BENGALI = "Bengali"
    KANNADA = "Kannada"
    MALAYALAM = "Malayalam"
    GUJARATI = "Gujarati"


class CustomerIntent(StrEnum):
    AGREE_TO_UPDATE_PAYMENT_METHOD = "agree_to_update_payment_method"
    REPORT_EXPIRED_CARD = "report_expired_card"
    REQUEST_CALLBACK = "request_callback"
    CLAIM_ALREADY_PAID = "claim_already_paid"
    ASK_FAILURE_REASON = "ask_failure_reason"
    BUSY_CALL_LATER = "busy_call_later"
    OPT_OUT = "opt_out"
    REQUEST_PAYMENT_LINK = "request_payment_link"
    NEEDS_CLARIFICATION = "needs_clarification"
    REFUSE_PAYMENT = "refuse_payment"
    DISPUTE_CHARGE = "dispute_charge"
    WRONG_PERSON = "wrong_person"
    OTHER = "other"


class AgentToolAction(StrEnum):
    GET_CUSTOMER_CONTEXT = "get_customer_context"
    GET_PAYMENT_FAILURE_DETAILS = "get_payment_failure_details"
    GET_PAYMENT_HISTORY = "get_payment_history"
    GET_PAYMENT_HISTORY_SUMMARY = "get_payment_history_summary"
    GET_CUSTOMER_NOTES = "get_customer_notes"
    REQUEST_PAYMENT_LINK = "request_payment_link"
    REQUEST_PAYMENT_METHOD_UPDATE_LINK = "request_payment_method_update_link"
    SCHEDULE_CALLBACK = "schedule_callback"
    MARK_DO_NOT_CONTACT = "mark_do_not_contact"
    LOG_PAYMENT_CLAIM_FOR_VERIFICATION = "log_payment_claim_for_verification"
    UPDATE_CONTACT_PREFERENCES = "update_contact_preferences"
    ADD_CUSTOMER_NOTE = "add_customer_note"
    UPDATE_CUSTOMER_NOTE = "update_customer_note"
    RECORD_CALL_OUTCOME = "record_call_outcome"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class NoteType(StrEnum):
    PAYMENT_CONTEXT = "payment_context"
    CUSTOMER_PREFERENCE = "customer_preference"
    CALLBACK = "callback"
    SUPPORT_CONTEXT = "support_context"
    AGENT_OBSERVATION = "agent_observation"


class NoteCreatedBy(StrEnum):
    SEED_DATA = "seed_data"
    VOICE_AGENT = "voice_agent"
    HUMAN_AGENT = "human_agent"


class ContactConsent(StrEnum):
    """`synthetic_test_record` implies NO real-world consent to contact anyone."""

    SYNTHETIC_TEST_RECORD = "synthetic_test_record"


# Workbook `Enums` sheet enum_name -> code enum. Checked by the importer.
WORKBOOK_ENUMS: dict[str, type[StrEnum]] = {
    "currency": Currency,
    "billing_frequency": BillingFrequency,
    "payment_status": PaymentStatus,
    "payment_history_status": PaymentHistoryStatus,
    "payment_failure_reason": PaymentFailureReason,
    "payment_method_type": PaymentMethodType,
    "recovery_status": RecoveryStatus,
    "call_status": CallStatus,
    "call_outcome": CallOutcome,
    "payment_link_status": PaymentLinkStatus,
    "supported_language": SupportedLanguage,
    "customer_intent": CustomerIntent,
    "agent_tool_action": AgentToolAction,
    "note_type": NoteType,
    "note_created_by": NoteCreatedBy,
    "contact_consent": ContactConsent,
}

# --- Backend-only enums -------------------------------------------------------------------


class TurnRole(StrEnum):
    AGENT = "agent"
    CUSTOMER = "customer"
    SYSTEM = "system"
    TOOL = "tool"


class ToolExecutionStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class CallbackStatus(StrEnum):
    REQUESTED = "requested"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PaymentLinkPurpose(StrEnum):
    PAYMENT = "payment"
    PAYMENT_METHOD_UPDATE = "payment_method_update"


class ActorType(StrEnum):
    SYSTEM = "system"
    IMPORTER = "importer"
    OPERATOR = "operator"
    VOICE_AGENT = "voice_agent"
    HUMAN_AGENT = "human_agent"
