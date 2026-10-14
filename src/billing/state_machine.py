"""Installment payment state machine. Status changes go through `transition()` only.

    SCHEDULED -> PROCESSING
    PROCESSING -> SUCCESS | FAILED | PENDING_RECONCILIATION
    PENDING_RECONCILIATION -> SUCCESS | FAILED          (trusted reconciliation result)
    FAILED -> RETRY_SCHEDULED | SUCCESS                  (retry policy | trusted confirmation)
    RETRY_SCHEDULED -> PROCESSING | SUCCESS              (next attempt | trusted confirmation)
    SUCCESS: terminal for the billing period.

FAILED with no retry scheduled is a resting state: only a trusted confirmation (ALREADY_PAID)
can still move it to SUCCESS. Retries never create a new installment; each is a new
`payment_attempt` row against the same installment.
"""

from enum import StrEnum


class InstallmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    PENDING_RECONCILIATION = "pending_reconciliation"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"  # bank timeout: not a failure until reconciled
    ALREADY_PAID = "already_paid"  # gateway reports the period already settled


class PaymentSource(StrEnum):
    AUTOPAY = "autopay"
    ALREADY_PAID_CONFIRMATION = "already_paid_confirmation"
    RECONCILIATION = "reconciliation"


S = InstallmentStatus
TRANSITIONS: dict[InstallmentStatus, frozenset[InstallmentStatus]] = {
    S.SCHEDULED: frozenset({S.PROCESSING}),
    S.PROCESSING: frozenset({S.SUCCESS, S.FAILED, S.PENDING_RECONCILIATION}),
    S.PENDING_RECONCILIATION: frozenset({S.SUCCESS, S.FAILED}),
    S.FAILED: frozenset({S.RETRY_SCHEDULED, S.SUCCESS}),
    S.RETRY_SCHEDULED: frozenset({S.PROCESSING, S.SUCCESS}),
    S.SUCCESS: frozenset(),
}
UNPAID = frozenset({S.FAILED, S.RETRY_SCHEDULED})


class InvalidTransitionError(Exception):
    def __init__(self, machine: str, current: str, target: str) -> None:
        super().__init__(f"invalid {machine} transition {current} -> {target}")
        self.current, self.target = current, target


def check_transition(current: InstallmentStatus, target: InstallmentStatus) -> InstallmentStatus:
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError("installment", current, target)
    return target
