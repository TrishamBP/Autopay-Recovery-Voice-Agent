# Autopay Recovery Backend

FastAPI backend for a **simulated** autopay / EMI payment-recovery voice agent. All data is synthetic,
payments are simulated, and **no call is ever placed without an explicit human approval** (the
default telephony provider is a mock that never dials).

| Topic | Guide |
|---|---|
| Schema, workbook import, money/time rules, read-only NL→SQL, typed tools | [docs/database.md](docs/database.md) |
| Virtual clock, event ledger, payment & recovery state machines, approval, telephony | [docs/simulation.md](docs/simulation.md) |
| Twilio webhooks, Alex conversation engine, transcripts, audit, retention | [docs/voice.md](docs/voice.md) |
| Project rules for AI coding agents | [.claude/CLAUDE.md](.claude/CLAUDE.md) |

## Architecture

```
HTTP (FastAPI, /api/v1, operator API key)          Twilio status webhook (signature-checked)
   │                                                    │
   ├─ customers / payment history / notes (read + audited note writes)
   ├─ database: summary + read-only NL→SQL (dev only) ── llm/ (Fireworks adapter, optional)
   ├─ simulation: create · state · advance · reset · events
   │     └─ processor: virtual clock + event ledger (one transaction per event, no network I/O)
   ├─ installments: list · detail · attempts · inject-scenario (dev only)
   └─ recovery: queue · case · approve · reject
         └─ approval: atomic claim → re-validate → telephony/ (mock | twilio) → record outcome
SQLite (aiosqlite, WAL) ── SQLAlchemy 2.0 async ── Alembic migrations ── audit_log
```

Modules live in `src/<domain>/` (`customers`, `payment_history`, `customer_notes`, `payments`,
`calls`, `tools`, `database_query`, `importers`, `billing`, `simulation`, `recovery`, `telephony`,
`llm`, `audit`, `evaluation`, `common`).

## Setup (run every command from `backend/`)

```bash
uv sync                                   # Python 3.13, dependencies from uv.lock
cp .env.example .env                      # then set OPERATOR_API_KEY (and optional values)
uv run alembic upgrade head               # create / migrate data/autopay_recovery.db
uv run python -m scripts.manage validate-workbook
uv run python -m scripts.manage import --dry-run
uv run python -m scripts.manage import    # idempotent; re-running reports no_changes
uv run python -m scripts.manage check     # integrity, FKs, derived-summary consistency
uv run python -m scripts.manage counts
uv run uvicorn src.main:app --reload      # http://127.0.0.1:8000/docs
```

The workbook is read from `../synthetic-data/data/autopay_recovery_customers.xlsx`
(`WORKBOOK_PATH`). SQLite URLs are relative to the working directory, so start the app from
`backend/`.

## Quality gate

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest            # isolated temp SQLite DBs; mocked Twilio + GLM; no network, no credentials
uv run alembic check     # models and migrations agree
```

No type checker is configured yet. Live-provider tests would be marked `@pytest.mark.live` and are
excluded by default; none exist yet.

## Demo workflow (curl)

`.env` for the demo: `OPERATOR_API_KEY=<key>`, `SIMULATION_DEV_ENDPOINTS_ENABLED=true`,
`TEST_DESTINATION_ALLOWLIST=+15005550006` (a number **you** control),
`ALLOW_SYNTHETIC_CONSENT_FOR_TEST_DESTINATIONS=true`, `TELEPHONY_PROVIDER=mock`.

```bash
B=http://127.0.0.1:8000/api/v1; H="X-API-Key: $OPERATOR_API_KEY"; J="Content-Type: application/json"

# 1. Create a simulation (idempotent) with explicit scenarios for some subscriptions
curl -s -X POST $B/simulation -H "$H" -H "$J" -d '{"simulation_id":"demo-001",
  "initial_time":"2026-10-01T00:00:00+05:30",
  "scenario_assignments":[{"subscription_id":"SUB-2001","scenario_id":"MANDATE_INACTIVE"},
                          {"subscription_id":"SUB-2003","scenario_id":"INSUFFICIENT_FUNDS"},
                          {"subscription_id":"SUB-2005","scenario_id":"BANK_TIMEOUT"}]}'

# 2. Advance virtual time (exactly one of advance_days / target_time). Never places calls.
curl -s -X POST $B/simulation/advance -H "$H" -H "$J" \
  -d '{"simulation_id":"demo-001","advance_days":10,"idempotency_key":"demo-advance-0001"}'
curl -s -X POST $B/simulation/advance -H "$H" -H "$J" \
  -d '{"simulation_id":"demo-001","target_time":"2026-10-12T18:30:00+05:30"}'

# 3. Inspect
curl -s "$B/simulation/state?simulation_id=demo-001" -H "$H"
curl -s "$B/simulation/events?simulation_id=demo-001&limit=50" -H "$H"
curl -s "$B/installments?simulation_id=demo-001" -H "$H"
curl -s "$B/recovery/queue?simulation_id=demo-001" -H "$H"
curl -s "$B/summary?simulation_id=demo-001" -H "$H"

# 4. Approve one queued case (mock call; destination must be allowlisted; virtual time must be
#    inside the customer's contact window and calling hours 09:00-20:00 IST)
curl -s -X POST $B/recovery/<case_id>/approve -H "$H" -H "$J" -H "Idempotency-Key: approve-0001" \
  -d '{"approved":true,"approved_by":"Demo Operator","approval_reason":"Approved for controlled demo",
       "test_destination":"+15005550006"}'

# 5. Simulate the mock call's status callback, inject a scenario, reset (dev only)
curl -s -X POST $B/simulation/calls/<call_session_id>/status -H "$H" -H "$J" -d '{"status":"completed","sequence":1}'
curl -s -X POST $B/installments/<installment_id>/inject-scenario -H "$H" -H "$J" \
  -d '{"scenario_id":"ALREADY_PAID","reason":"customer showed bank confirmation"}'
curl -s -X POST $B/simulation/reset -H "$H" -H "$J" -d '{"simulation_id":"demo-001","confirm":"RESET demo-001"}'
```

Example advance response (actual values from a run on the imported dataset):

```json
{"simulation_id":"demo-001","status":"completed","previous_time":"2026-10-01T00:00:00+05:30",
 "current_time":"2026-10-11T00:00:00+05:30","events_created":38,"events_processed":35,
 "installments_created":10,"payment_attempts_created":10,"recovery_cases_created":2,
 "recovery_cases_queued":2,"calls_initiated":0,"replayed":false}
```

## Voice calls with Twilio (controlled live test)

> **Warnings.** Live calls cost money and Twilio trial accounts can only call **verified** numbers
> and play a trial notice. Only ever call a number **you own**: put it in
> `TWILIO_ALLOWED_TEST_NUMBERS`; every other number (including all customer-record numbers) is
> refused. Keep credentials in `.env` only, never in code, chat or screenshots; rotate any that
> were exposed. Nothing here has been verified with a live call yet: do that yourself in a
> controlled test.

### 1. Install and run locally (Windows PowerShell, from `backend\`)

```powershell
uv sync
Copy-Item .env.example .env        # then edit .env: OPERATOR_API_KEY, FIREWORKS_*, TWILIO_*
uv run alembic upgrade head
uv run python -m scripts.manage import
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000
```

### 2. Expose it over HTTPS (second PowerShell window)

```powershell
ngrok http 8000                    # copy the https://….ngrok-free.app URL
```

Set `PUBLIC_BASE_URL=https://<that-host>` in `.env` **exactly** (no trailing path): webhook
URLs and signature validation are built from it. Restart uvicorn after editing `.env`. A new
ngrok URL means updating `PUBLIC_BASE_URL` again.

### 3. Twilio Console (manual steps)

1. **Verify your test phone** (trial accounts): Console → *Phone Numbers → Manage → Verified
   Caller IDs* → add your mobile and confirm the code. Put the same E.164 number in
   `TWILIO_ALLOWED_TEST_NUMBERS`.
2. Buy or choose a voice-capable Twilio number → `TWILIO_FROM_NUMBER`.
3. Copy Account SID and Auth Token (Console → *Account → API keys & tokens*) into `.env`.
4. No number-level webhook is required for outbound calls: every call is created with absolute
   `url=<PUBLIC_BASE_URL>/api/v1/twilio/voice?...` and
   `status_callback=<PUBLIC_BASE_URL>/api/v1/twilio/status?...`.
5. Enable live mode in `.env`: `TELEPHONY_PROVIDER=twilio`, `TWILIO_ENABLED=true`,
   `ALLOW_SYNTHETIC_CONSENT_FOR_TEST_DESTINATIONS=true` (demo only). Restart.

```powershell
$B = "http://127.0.0.1:8000/api/v1"
$H = @{ "X-API-Key" = $env:OPERATOR_API_KEY }   # or paste your key
Invoke-RestMethod "$B/twilio/health"            # live_calls_possible should be true
```

### 4. Place one approved test call

```powershell
Invoke-RestMethod -Method Post "$B/simulation" -Headers $H -ContentType "application/json" `
  -Body '{"simulation_id":"live-001","scenario_assignments":[{"subscription_id":"SUB-2001","scenario_id":"MANDATE_INACTIVE"}]}'
Invoke-RestMethod -Method Post "$B/simulation/advance" -Headers $H -ContentType "application/json" `
  -Body '{"simulation_id":"live-001","target_time":"2026-10-12T18:30:00+05:30"}'
$queue = Invoke-RestMethod "$B/recovery/queue?simulation_id=live-001" -Headers $H
$case = $queue.items[0].recovery_case_id
Invoke-RestMethod -Method Post "$B/recovery/$case/approve" -Headers ($H + @{ "Idempotency-Key" = "live-approve-0001" }) `
  -ContentType "application/json" `
  -Body '{"approved":true,"approved_by":"Your Name","approval_reason":"Controlled live test","test_destination":"+91XXXXXXXXXX"}'
```

The virtual time must be inside the customer's contact window (CUS-1001: weekdays 6–8 PM IST)
and calling hours. Answer the phone and say "Yes, speaking", then "Sure", then try "Can you send
me a payment link?", "I already paid", "Call me back tomorrow at 6 PM" or "Stop calling me".

### 5. Inspect results

```powershell
$calls = Invoke-RestMethod "$B/calls?simulation_id=live-001" -Headers $H
Invoke-RestMethod "$B/calls/$($calls.items[0].call_id)/transcript" -Headers $H
Invoke-RestMethod "$B/calls/$($calls.items[0].call_id)/audit" -Headers $H
```

Twilio side: Console → *Monitor → Logs → Calls* (call status, duration) and *Monitor → Logs →
Errors* (webhook failures such as 11200/11205/12100). Application side: JSON logs in the uvicorn
window (`correlation_id`, `call_session_id`, intents; never transcript text or secrets).

Testing GLM without a phone: keep `TELEPHONY_PROVIDER=mock`, set
`TWILIO_ALLOW_UNSIGNED_WEBHOOKS=true` and **no** `TWILIO_AUTH_TOKEN`, approve a case (mock call),
then post form data to `/api/v1/twilio/voice?call_session_id=<id>` and
`/api/v1/twilio/process-speech?call_session_id=<id>&turn=1` with `CallSid=<provider_call_id>`
and `SpeechResult=...`.

### If an approval times out

`call_initiation: "unknown"` means Twilio may have created the call. Do not re-approve; run
`POST /api/v1/recovery/{case}/reconcile-call` with `{"test_destination": "+91…"}` (it looks the
call up; after `TELEPHONY_RECONCILE_GRACE_SECONDS` with no call found the case becomes
`call_failed` and a new explicit approval is allowed).

## Deploying to Render

`data/seed.db` is committed: the imported synthetic workbook (10 customers, history, notes) at
the current Alembic head, with no calls, transcripts or simulation state. Local working DBs are
git-ignored because they hold live-test history.

- Web Service, **one instance** (deferred voice turns and per-call locks are in-process), on a
  paid plan (a sleeping free instance wakes slower than Twilio's webhook timeout).
- Build `uv sync --frozen`; pre-deploy `uv run alembic upgrade head`;
  start `uv run uvicorn src.main:app --host 0.0.0.0 --port $PORT`.
- Environment: everything from `.env.example`, with `DATABASE_URL=sqlite+aiosqlite:///./data/seed.db`
  and `PUBLIC_BASE_URL=https://<service>.onrender.com`. Without a persistent disk the DB resets to
  the seed on every deploy/restart; for durable history attach a disk and point `DATABASE_URL` at it.
- Twilio trial: destinations must be Verified Caller IDs and in `TWILIO_ALLOWED_TEST_NUMBERS`;
  sandbox webhooks are unsigned, so the trial exception needs `APP_ENV=development` (the default).
  After upgrading, set `TWILIO_TRIAL_ACCOUNT=false` and `TWILIO_TRIAL_ACCEPT_UNSIGNED_WEBHOOKS=false`.

Rebuild the seed after a schema or workbook change (PowerShell, from `backend\`):

```powershell
Remove-Item data\seed.db -ErrorAction SilentlyContinue
$env:DATABASE_URL = "sqlite+aiosqlite:///./data/seed.db"
uv run alembic upgrade head
uv run python -m scripts.manage import
uv run python -m scripts.manage check
Remove-Item Env:DATABASE_URL
```

## Known limitations

- SQLite: one writer at a time; advances are serialised per simulation inside one process. Run a
  single app process (no multi-worker uvicorn) until PostgreSQL is adopted (see docs/database.md).
- One subscription per customer (derived from the workbook); weekly billing is not supported
  because the dataset's `billing_frequency` enum only has monthly/annual.
- Simulated payment links are created but not delivered (no delivery channel yet).
- Voice is turn-based `<Gather>`/`<Say>` in English (`en-IN`); Media Streams and other
  languages are not implemented. Live calling has not been verified end to end.
- No human-agent transfer or payment-link delivery channel; those requests are recorded only.
- No type checker in the quality gate yet.

## Troubleshooting

| Symptom (voice) | Fix |
|---|---|
| Twilio error 11200 / call says "application error" | `PUBLIC_BASE_URL` wrong or tunnel down; check `GET /api/v1/twilio/health` and the ngrok window. |
| Webhooks answer `403 invalid_signature` | `PUBLIC_BASE_URL` must match the URL Twilio calls exactly (scheme, host, no trailing slash); auth token must be the account's. |
| Call is placed but hangs up immediately | The session failed verification (wrong CallSid/AccountSid, case no longer active) or the customer is opted out. Check app logs. |
| `409 approval_denied` | Read `details.reason`: `destination_not_allowlisted`, `destination_is_customer_record`, `outside_contact_window`, `consent_invalid`, … |
| Trial account: "number unverified" | Verify the destination under *Verified Caller IDs*. |
| Agent says "having a little trouble" | GLM timed out or returned invalid JSON; check `FIREWORKS_*`, `VOICE_LLM_TIMEOUT_SECONDS`, logs (`voice llm unavailable`). |


| Symptom | Fix |
|---|---|
| `503 Operator API is disabled` | Set `OPERATOR_API_KEY` in `.env`. |
| `no such table` / `Database schema not initialised` | `uv run alembic upgrade head` from `backend/`. |
| Import `blocked` | Read the report's `conflicts` / `discrepancies`; nothing was written. |
| `409 advance_incomplete` | An event failed; re-issue the advance. If dead-lettered, `POST /simulation/events/{id}/retry`. |
| Approval `409` with `reason` | The reason code says which rule failed (`outside_contact_window`, `consent_invalid`, …). |
| NL query `503 llm_not_configured` | Set `FIREWORKS_API_KEY`, `FIREWORKS_MODEL`, `SQL_QUERY_ENABLED=true`. |
