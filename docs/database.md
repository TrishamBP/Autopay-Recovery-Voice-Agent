# Database, import and safe query layer

## Schema

SQLite (`sqlite+aiosqlite`) via SQLAlchemy 2.0 async; schema owned by Alembic
(`95d80b8b9081` initial, `45aa7bf826d6` simulation/billing/recovery). Constraint names follow a
fixed naming convention; enums are `VARCHAR` + `CHECK` (portable to PostgreSQL).

| Table | Key | Purpose |
|---|---|---|
| `customer` | `customer_id` (`CUS-1001`) | Identity, contact prefs, consent marker, DNC, current failed charge, call cache, **derived** 6-month summary + notes digest |
| `payment_history` | `history_id`; unique `(customer_id, billing_month)` | One row per billing cycle (final outcome) |
| `customer_note` | `note_id` | Canonical notes (untrusted text) |
| `call_scenario` | `customer_id` (1:1) | Workbook `Call_Scenarios`, **evaluation only** (hidden from agent, tools and SQL) |
| `call_session`, `conversation_turn`, `callback_request` | UUID | Calls (one active call per recovery case), turns, callbacks |
| `tool_execution` | UUID; unique `(tool_name, idempotency_key)` | Every typed-tool call, incl. rejections |
| `payment_link` | UUID; unique `token` | SIMULATED links (`https://payments.example.invalid/...`) |
| `audit_log` | UUID | Every mutation and import run |
| `subscription`, `billing_installment`, `payment_attempt`, `recovery_case`, `simulation_*`, `scenario_assignment` | | See [simulation.md](simulation.md) |

Deletion: customers are `RESTRICT` everywhere (financial/audit history); turns `CASCADE` with their
call; optional call references `SET NULL`.

## Representations

- **Money**: integer **paise** in `*_paise` / `amount_minor_units` columns (INR 799 → `79900`);
  `Decimal` with 2 places in code; strings like `"799.00"` in JSON. No floats.
- **Time**: `UTCDateTime` columns reject naive datetimes and store UTC (naive-UTC text on SQLite,
  `timestamptz` on PostgreSQL). Dates stay `DATE`. API timestamps are ISO 8601 (`Z` or local offset).
- **Booleans** are real booleans; counts are integers; currency code (`INR`) is separate from amounts.

## Workbook → database mapping

| Workbook | Table | Notes |
|---|---|---|
| `Customers` (38 cols) | `customer` (+ `subscription`) | `payment_amount` → `payment_amount_paise`; IST timestamps → UTC; phone normalised to E.164 |
| `Payment_History` | `payment_history` | `payment_status` → `status`, `payment_method_type` → `payment_method`, amounts → paise |
| `Customer_Notes` | `customer_note` | |
| `Call_Scenarios` | `call_scenario` | evaluation data |
| `Enums` | — | Verified to match the backend enums exactly (mismatch = error) |
| `README` | — | Documentation, not imported |

`customer_contact_consent = synthetic_test_record` is stored as-is and is **not** consent.

## Payment-history status semantics

| status | amount_paid | payment_date / days_late | counts as fully paid |
|---|---|---|---|
| `paid_on_time` | = due | ≤ due date / 0 | yes |
| `paid_late` | = due | > due date / ≥ 1 | yes |
| `partially_paid` | 0 < x < due | set / days to receipt | **no** |
| `failed` | 0 | null | no — open latest cycle only |
| `missed` | 0 | null | no — closed cycle only |

`status` is the cycle's final outcome; intermediate attempts are captured by `failure_reason`,
`retry_count` (failed retries) and `recovered_after_failure`. Enforced by 15 CHECK constraints and
by the importer.

## Derived summaries (`src/payment_history/summary.py`)

Computed from the customer's 6 most recent cycles (n ≤ 6), never taken from the workbook:
on-time/late/failed/missed counts; `minimum_amount_paid_6m` = smallest positive amount (partials
included, null if none); `total_amount_paid_6m` = sum received;
`payment_reliability_score` = round-half-up(100 × (on_time + late) / n), null when n = 0 —
an operational indicator, **not a credit score**. The notes digest (`customer.customer_notes`,
`notes_updated_at`) is rewritten in the same transaction as every note change.
`scripts.manage check` verifies stored caches equal recomputed values.

## Import pipeline (`src/importers/`)

1. read workbook (thread) → 2. sheets/columns (missing or unmapped columns = error) →
3–4. typed Pydantic row models (strict cell coercion) → 5. uniqueness, FKs, cycle rules,
current-charge ↔ latest-cycle consistency, enum sheet → 6. report (errors, warnings,
discrepancies) → 7. plan by natural key and apply in **one transaction** → 8. recompute summaries
→ 9. report insert / update / unchanged / conflict / rejected / not_in_workbook.

Rules: unchanged input → `no_changes` (nothing written); a changed existing record → **blocked**
unless `--allow-updates` (audited), which still never resets `do_not_contact` true → false;
nothing is ever deleted; summary discrepancies block unless `--accept-computed-summaries`;
`--dry-run` plans without writing.

Result on the supplied workbook: 0 errors, 0 warnings, **0 discrepancies**; imported
10 customers, 10 subscriptions, 60 history rows, 25 notes, 10 scenarios.

## SQLite settings and the PostgreSQL path

Per connection: `foreign_keys=ON`, `busy_timeout=5000` ms, `journal_mode=WAL`, `synchronous=NORMAL`.
WAL lets readers run alongside the single writer; it needs a local disk. Write transactions are
short and never span network calls. To move to PostgreSQL: switch `DATABASE_URL` to
`postgresql+asyncpg://…`, add `asyncpg`, run the migrations; partial indexes and `with_for_update`
already carry PostgreSQL variants. The read-only query engine is SQLite-specific (`mode=ro`) and
would become a read-only database role.

## Natural-language read-only SQL (`POST /api/v1/database/query`)

Enabled only with `SQL_QUERY_ENABLED=true`, `APP_ENV` development/test and the operator key.
It is an operator analytics tool, **never exposed to the voice agent** (the workbook README forbids
LLM-generated SQL for the agent).

Boundaries, in order — each assumes the previous can fail:
1. LLM (Fireworks) sees only the allowlisted schema and returns JSON `{"sql": …}`.
2. `sql_validator.py` parses with SQLGlot: one statement; SELECT / set-op only; forbidden node
   types (DML, DDL, PRAGMA, ATTACH, transactions, `SELECT INTO`, locks, recursive CTEs); function
   allowlist (unknown functions such as `load_extension` rejected); no schema-qualified or
   non-allowlisted tables; every column resolved against an **allowlist-only** schema (`*` expands
   to safe columns; `phone_number`, `email`, link tokens, audit tables, scenarios are invisible);
   forced `LIMIT`; literals moved into bound parameters; SQL regenerated from the AST.
3. Execution on a separate engine opened `mode=ro` with `PRAGMA query_only=ON`, a progress-handler
   deadline (`SQL_TIMEOUT_SECONDS`) and `SQL_MAX_ROWS`.
4. Optional explanation sees only the returned rows, delimited as untrusted data.

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/database/query -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question":"Which customers have a failed autopay and are eligible for retry?","limit":50}'
```

Response: `question`, `query.validated_read_only_sql` + `parameters` + `tables`, `columns`,
`rows`, `row_count`, `truncated`, `duration_ms`, `explanation`, `explanation_status`. Errors:
`422 query_rejected` / `query_timeout`, `502 llm_*`, `503 llm_not_configured`, `504 llm_timeout`.

## Typed mutation tools (`src/tools/`)

The LLM picks from a fixed registry: `get_payment_status`, `get_payment_history_summary`,
`add_customer_note`, `schedule_callback`, `mark_do_not_contact` (one-way),
`request_payment_link` (simulated). Arguments are Pydantic models with `extra="forbid"` (an
LLM-supplied `customer_id` is rejected); the customer and call come from server-side
`ToolContext`. Side-effecting tools require an idempotency key (replays return the stored result).
The mutation, its audit events and the `tool_execution` row commit together; rejections and
failures are recorded separately and never reported as success. Do-not-contact is enforced by
`customers.service.assert_contactable`, independent of any prompt.
