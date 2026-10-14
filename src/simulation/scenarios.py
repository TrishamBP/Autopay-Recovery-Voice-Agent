"""Deterministic payment-scenario catalog (configuration, separate from attempt records).

A scenario fixes the outcome of attempt N of an installment. It is chosen once when the
installment is created and stored on it, so replaying events can never change an outcome.
Selection: explicit `scenario_assignment` (period, then '*'), else a seeded hash of
(seed, subscription_id, billing_period) over `DEFAULT_WEIGHTS`. No unseeded randomness.
Nothing here moves money: these are simulated outcomes only.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from src.billing.state_machine import AttemptOutcome as O
from src.common.enums import PaymentFailureReason as F


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    attempt_outcomes: tuple[O, ...]  # outcome of attempt 1, 2, ... (last value repeats)
    failure_reason: F | None = None
    retry_permitted: bool = False
    retry_delay: timedelta = timedelta(0)
    max_attempts: int = 1
    recovery_delay: timedelta = timedelta(days=1)
    reconciliation_delay: timedelta | None = None
    reconciled_outcome: O | None = None

    def outcome_for_attempt(self, attempt_number: int) -> O:
        return self.attempt_outcomes[min(attempt_number, len(self.attempt_outcomes)) - 1]


CATALOG: dict[str, Scenario] = {
    s.scenario_id: s
    for s in (
        Scenario("SUCCESSFUL_PAYMENT", "Autopay succeeds; no recovery.", (O.SUCCEEDED,)),
        Scenario(
            "MANDATE_INACTIVE",
            "Mandate inactive: definitive failure, no automatic retry.",
            (O.FAILED,),
            F.MANDATE_INACTIVE,
        ),
        Scenario(
            "INSUFFICIENT_FUNDS",
            "Insufficient funds on every attempt; 2 retries 48h apart.",
            (O.FAILED,),
            F.INSUFFICIENT_FUNDS,
            retry_permitted=True,
            retry_delay=timedelta(hours=48),
            max_attempts=3,
        ),
        Scenario(
            "INSUFFICIENT_FUNDS_RECOVERS",
            "Fails once, the 48h retry succeeds.",
            (O.FAILED, O.SUCCEEDED),
            F.INSUFFICIENT_FUNDS,
            retry_permitted=True,
            retry_delay=timedelta(hours=48),
            max_attempts=3,
        ),
        Scenario(
            "BANK_TIMEOUT",
            "Bank timeout: outcome unknown; reconciled as failed after 48h.",
            (O.UNKNOWN,),
            F.TECHNICAL_ERROR,
            reconciliation_delay=timedelta(hours=48),
            reconciled_outcome=O.FAILED,
        ),
        Scenario(
            "ALREADY_PAID",
            "Trusted confirmation that the period is already paid.",
            (O.ALREADY_PAID,),
        ),
    )
}

DEFAULT_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("SUCCESSFUL_PAYMENT", 4),
    ("INSUFFICIENT_FUNDS", 2),
    ("MANDATE_INACTIVE", 1),
    ("BANK_TIMEOUT", 1),
    ("INSUFFICIENT_FUNDS_RECOVERS", 1),
    ("ALREADY_PAID", 1),
)


def seeded_scenario_id(seed: int, subscription_id: str, billing_period: str) -> str:
    digest = hashlib.sha256(f"{seed}:{subscription_id}:{billing_period}".encode()).digest()
    total = sum(w for _, w in DEFAULT_WEIGHTS)
    point = int.from_bytes(digest[:8], "big") % total
    for scenario_id, weight in DEFAULT_WEIGHTS:
        if point < weight:
            return scenario_id
        point -= weight
    raise AssertionError("unreachable")  # pragma: no cover
