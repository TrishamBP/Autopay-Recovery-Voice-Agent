"""Event taxonomy and deterministic idempotency keys.

Key formats (stable; never contain wall-clock time):
    installment:{simulation_id}:{subscription_id}:{billing_period}
    payment-attempt:{installment_id}:{attempt_number}
    recovery-case:{installment_id}
    event:{simulation_id}:{EVENT_TYPE}:{aggregate_id}:{discriminator}
"""

from enum import StrEnum


class SimulationStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class EventType(StrEnum):
    # processed by the clock (scheduled at a virtual time)
    INSTALLMENT_DUE = "INSTALLMENT_DUE"
    PAYMENT_ATTEMPT_REQUESTED = "PAYMENT_ATTEMPT_REQUESTED"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_RECONCILIATION_REQUIRED = "PAYMENT_RECONCILIATION_REQUIRED"
    PAYMENT_RECONCILIATION_RESOLVED = "PAYMENT_RECONCILIATION_RESOLVED"
    RETRY_DUE = "RETRY_DUE"
    RECOVERY_ELIGIBLE = "RECOVERY_ELIGIBLE"
    RECOVERY_APPROVAL_REQUIRED = "RECOVERY_APPROVAL_REQUIRED"
    # recorded facts (written already processed, for a complete ledger)
    RECOVERY_CASE_OPENED = "RECOVERY_CASE_OPENED"
    RECOVERY_CANCELLED = "RECOVERY_CANCELLED"
    RECOVERY_APPROVED = "RECOVERY_APPROVED"
    RECOVERY_REJECTED = "RECOVERY_REJECTED"
    CALL_INITIATION_REQUESTED = "CALL_INITIATION_REQUESTED"
    CALL_INITIATION_FAILED = "CALL_INITIATION_FAILED"
    CALL_STATUS_UPDATED = "CALL_STATUS_UPDATED"
    SCENARIO_INJECTED = "SCENARIO_INJECTED"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    SKIPPED = "skipped"  # processed as a no-op (e.g. retry for an already-paid installment)
    FAILED = "failed"  # dead-lettered after max attempts; blocks the clock


class AdvanceStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"  # a dead-lettered event stops the clock before it
    INTERRUPTED = "interrupted"  # superseded by a later advance after a crash


def installment_key(simulation_id: str, subscription_id: str, period: str) -> str:
    return f"installment:{simulation_id}:{subscription_id}:{period}"


def attempt_key(installment_id: str, attempt_number: int) -> str:
    return f"payment-attempt:{installment_id}:{attempt_number}"


def recovery_case_key(installment_id: str) -> str:
    return f"recovery-case:{installment_id}"


def event_key(
    simulation_id: str, event_type: EventType, aggregate_id: str, discriminator: str
) -> str:
    return f"event:{simulation_id}:{event_type.value}:{aggregate_id}:{discriminator}"
