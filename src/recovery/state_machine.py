"""Recovery-case state machine (distinct from the payment state machine).

    OPEN -> ELIGIBLE                         eligible_at reached (virtual time)
    ELIGIBLE -> AWAITING_APPROVAL            queued for a human; no call is placed
    ELIGIBLE -> OPTED_OUT                    customer is do-not-contact at eligibility time
    AWAITING_APPROVAL -> APPROVED            operator approval claimed atomically
    AWAITING_APPROVAL -> REJECTED            operator rejected
    APPROVED -> CALL_INITIATED               provider accepted the call
    APPROVED -> CALL_INITIATION_UNKNOWN      provider timed out: outcome unknown, NOT retried
    APPROVED -> CALL_FAILED                  provider definitively refused
    CALL_INITIATION_UNKNOWN -> CALL_INITIATED | CALL_FAILED   (reconciled by status callback)
    CALL_INITIATED -> CALL_COMPLETED | CALL_FAILED            (status callback)
    CALL_FAILED | CALL_COMPLETED -> APPROVED | REJECTED       (a follow-up needs a NEW approval)
    any active -> CANCELLED                  payment succeeded / already paid (no call made)
    any active -> CLOSED                     payment received after contact (outcome recorded)
    any active -> OPTED_OUT                  customer asked for no further contact

Terminal: CANCELLED, CLOSED, OPTED_OUT, REJECTED. A call outcome never implies payment success:
only a payment event closes or cancels a case.
"""

from enum import StrEnum

from src.billing.state_machine import InvalidTransitionError


class RecoveryCaseStatus(StrEnum):
    OPEN = "open"
    ELIGIBLE = "eligible"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CALL_INITIATED = "call_initiated"
    CALL_INITIATION_UNKNOWN = "call_initiation_unknown"
    CALL_FAILED = "call_failed"
    CALL_COMPLETED = "call_completed"
    CANCELLED = "cancelled"
    CLOSED = "closed"
    OPTED_OUT = "opted_out"
    REJECTED = "rejected"


class RecoveryOutcome(StrEnum):
    PAYMENT_SUCCEEDED = "payment_succeeded"  # paid before any contact
    ALREADY_PAID = "already_paid"
    PAYMENT_RECEIVED_AFTER_CONTACT = "payment_received_after_contact"
    DO_NOT_CONTACT = "do_not_contact"
    REJECTED_BY_OPERATOR = "rejected_by_operator"


R = RecoveryCaseStatus
TERMINAL = frozenset({R.CANCELLED, R.CLOSED, R.OPTED_OUT, R.REJECTED})
ACTIVE = frozenset(set(R) - TERMINAL)
APPROVABLE = frozenset({R.AWAITING_APPROVAL, R.CALL_FAILED, R.CALL_COMPLETED})
CONTACTED = frozenset(
    {R.CALL_INITIATED, R.CALL_INITIATION_UNKNOWN, R.CALL_COMPLETED, R.CALL_FAILED}
)

_ENDINGS = frozenset({R.CANCELLED, R.CLOSED, R.OPTED_OUT})
TRANSITIONS: dict[RecoveryCaseStatus, frozenset[RecoveryCaseStatus]] = {
    R.OPEN: frozenset({R.ELIGIBLE}) | _ENDINGS,
    R.ELIGIBLE: frozenset({R.AWAITING_APPROVAL}) | _ENDINGS,
    R.AWAITING_APPROVAL: frozenset({R.APPROVED, R.REJECTED}) | _ENDINGS,
    R.APPROVED: frozenset({R.CALL_INITIATED, R.CALL_INITIATION_UNKNOWN, R.CALL_FAILED}) | _ENDINGS,
    R.CALL_INITIATION_UNKNOWN: frozenset({R.CALL_INITIATED, R.CALL_FAILED}) | _ENDINGS,
    R.CALL_INITIATED: frozenset({R.CALL_COMPLETED, R.CALL_FAILED}) | _ENDINGS,
    R.CALL_FAILED: frozenset({R.APPROVED, R.REJECTED}) | _ENDINGS,
    R.CALL_COMPLETED: frozenset({R.APPROVED, R.REJECTED}) | _ENDINGS,
    **{t: frozenset() for t in TERMINAL},
}


def check_transition(current: RecoveryCaseStatus, target: RecoveryCaseStatus) -> RecoveryCaseStatus:
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError("recovery case", current, target)
    return target
