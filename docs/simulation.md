# Virtual-time payment-recovery simulation

## Clock

`simulation_state` stores each simulation's `current_virtual_time` (UTC), `timezone`
(default `Asia/Kolkata`), `initial_virtual_time`, `seed`, `configuration_version` and
`generation`. Process memory never holds authoritative time. Domain logic reads virtual time from
events/state; wall-clock time is used only for operational timestamps (`processed_at_real`,
`approved_at`) and timeouts.

- `advance_days` adds calendar days in the simulation time zone; `target_time` must carry an offset;
  sending both (or neither) is a 422. Backward targets are 409; reset is the only way back.
- An advance with an `idempotency_key` is replayed (same result, no second advance) when retried.

## Event ledger and processing

`simulation_event` rows have a deterministic `idempotency_key` (unique per simulation) and a
per-simulation `sequence`. Processing order: `(scheduled_at_virtual, sequence)`.

```
INSTALLMENT_DUE (local 00:00 of due date)
  └─ PAYMENT_ATTEMPT_REQUESTED (local 11:00, SIMULATION_AUTOPAY_TIME)
       ├─ PAYMENT_SUCCEEDED ── ends any active recovery case
       ├─ PAYMENT_FAILED ── RETRY_DUE (+retry delay) ─► PAYMENT_ATTEMPT_REQUESTED (n+1)
       │                └─ RECOVERY_CASE_OPENED ─► RECOVERY_ELIGIBLE (+recovery delay)
       │                                           └─ RECOVERY_APPROVAL_REQUIRED ─► queue
       └─ PAYMENT_RECONCILIATION_REQUIRED ─► PAYMENT_RECONCILIATION_RESOLVED (+48h) ─► SUCCEEDED/FAILED
Recorded facts (written processed): RECOVERY_CASE_OPENED, RECOVERY_CANCELLED, RECOVERY_APPROVED,
RECOVERY_REJECTED, CALL_INITIATION_REQUESTED, CALL_INITIATION_FAILED, CALL_STATUS_UPDATED,
SCENARIO_INJECTED.
```

Key formats: `installment:{sim}:{subscription}:{period}`, `payment-attempt:{installment}:{n}`,
`recovery-case:{installment}`, `event:{sim}:{TYPE}:{aggregate}:{discriminator}` (no timestamps).

### Transaction and crash-recovery strategy

1. One transaction materialises `INSTALLMENT_DUE` events for every due date in
   `[initial_time, target]` (insert-if-absent) and records a `simulation_advance` run.
2. Then **one transaction per event**: claim it with a conditional UPDATE (takes SQLite's write
   lock first), apply effects, insert follow-ups (never earlier than the cause — a scheduling bug
   raises instead of looping), mark processed, move the clock to the event time, update counters,
   commit.
3. With nothing ≤ target left, the clock moves to the target and the run completes.

A crash leaves the clock at the last fully applied event; re-issuing the advance resumes and,
thanks to idempotency keys, creates no duplicates. A failing event is rolled back and its
`attempt_count`/`last_error` recorded; after `SIMULATION_MAX_EVENT_ATTEMPTS` it is dead-lettered
and the clock refuses to pass it (409) until `POST /simulation/events/{id}/retry`. A cap on events
per advance guards against cycles. Reset bumps `generation`; an in-flight advance from an older
generation aborts. The processor makes no LLM or telephony calls.

## Billing rules

Monthly due on the anchor day, clamped to month end (31st → 30th / 28th / 29th in leap years);
annual Feb 29 → Feb 28 in non-leap years. One installment per `(simulation, subscription, period)`;
retries add `payment_attempt` rows to the same installment. Weekly billing is not supported (not in
the dataset enum).

## Scenarios (deterministic)

| Scenario | Attempt outcomes | Retry | Recovery |
|---|---|---|---|
| `SUCCESSFUL_PAYMENT` | succeeded | – | none |
| `MANDATE_INACTIVE` | failed (mandate_inactive) | no | eligible +24h |
| `INSUFFICIENT_FUNDS` | failed ×3 | +48h, max 3 | eligible +24h after first failure |
| `INSUFFICIENT_FUNDS_RECOVERS` | failed, then succeeded | +48h | case cancelled on success |
| `BANK_TIMEOUT` | unknown → reconciled failed after 48h | no | only after reconciliation |
| `ALREADY_PAID` | trusted confirmation | – | cancels pending recovery |

Selection: explicit `scenario_assignment` for the period, else for all periods (`*`), else a SHA-256
hash of `(seed, subscription, period)` over fixed weights. The chosen scenario is stored on the
installment, so replays never change outcomes. `inject-scenario` (dev only) changes future
attempts of a non-final installment, or applies `ALREADY_PAID` as a trusted confirmation; it is
refused for paid installments.

## Payment state machine

```
SCHEDULED → PROCESSING → SUCCESS | FAILED | PENDING_RECONCILIATION
PENDING_RECONCILIATION → SUCCESS | FAILED
FAILED → RETRY_SCHEDULED | SUCCESS (trusted confirmation)
RETRY_SCHEDULED → PROCESSING | SUCCESS (trusted confirmation)
SUCCESS: terminal
```

## Recovery state machine

```
OPEN → ELIGIBLE → AWAITING_APPROVAL → APPROVED → CALL_INITIATED → CALL_COMPLETED | CALL_FAILED
                                      APPROVED → CALL_INITIATION_UNKNOWN → CALL_INITIATED | CALL_FAILED
CALL_FAILED | CALL_COMPLETED → APPROVED (new approval) | REJECTED
any active → CANCELLED (paid, no contact) | CLOSED (paid after contact) | OPTED_OUT (do-not-contact)
AWAITING_APPROVAL → REJECTED
Terminal: CANCELLED, CLOSED, OPTED_OUT, REJECTED
```

At most one active case per installment (partial unique index). A call outcome never changes
payment status.

## Manual approval (`POST /recovery/{id}/approve`)

The only code path that initiates a call. Steps: destination allowlist check → atomic claim
(conditional UPDATE) → re-validation in the same transaction (eligible in virtual time, installment
unpaid and not pending reconciliation, not do-not-contact, consent basis, contact window +
calling hours at the **virtual** time) → `call_session` row (one active call per case) → short
pre-call re-check → provider call outside any transaction → record outcome. Denials return 409 with
`details.reason`; side effects (opt-out, cancellation) and an audit row are persisted.

Consent: synthetic records are accepted only with
`ALLOW_SYNTHETIC_CONSENT_FOR_TEST_DESTINATIONS=true` **and** an allowlisted tester number.
Timeouts are recorded as `CALL_INITIATION_UNKNOWN` and reconciled by the signed status callback,
never re-dialled automatically. `Idempotency-Key` on approve replays the original result.

## Telephony

`TELEPHONY_PROVIDER=mock` (default) records calls in memory and dials nothing. Twilio uses the
official SDK in a worker thread with a timeout; status callbacks at
`/api/v1/twilio/status` are validated with `RequestValidator` against
`PUBLIC_BASE_URL`, and duplicate / out-of-order statuses are ignored (`SequenceNumber`, monotonic
status rank).
