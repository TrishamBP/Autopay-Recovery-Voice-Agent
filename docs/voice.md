# Voice agent: Twilio webhooks, conversation engine, transcripts

Turn-based: Twilio `<Gather input="speech">` recognises speech, `<Say>` speaks. No Media Streams.

## Request flow

```
POST /api/v1/recovery/{case}/approve   (operator; the ONLY way a call starts)
  -> Twilio Calls API (url=/twilio/voice, status_callback=/twilio/status, time_limit)
POST /api/v1/twilio/voice?call_session_id=…            callee answered -> opening + <Gather>
POST /api/v1/twilio/process-speech?call_session_id=…&turn=N   SpeechResult -> reply + <Gather>
POST /api/v1/twilio/status?call_session_id=…           ringing / in-progress / completed / …
GET  /api/v1/twilio/health                             non-sensitive configuration status
```

Every webhook is checked with the official SDK `RequestValidator` against
`PUBLIC_BASE_URL + path + query`. `X-Forwarded-*` headers are ignored. The session must belong
to an approved recovery call, and the request's `CallSid` (and `AccountSid` in live mode) must
match it. Requests that fail verification get a bare `<Hangup/>` with no account details.

## Conversation engine (`src/voice/`)

| File | Role |
|---|---|
| `prompts.py` | **System prompt (`SYSTEM_PROMPT`) and scripts.** Edit here to adjust Alex. |
| `vocabulary.py` | Stages, intents, preferences, allowlisted actions, event types |
| `schemas.py` | `AgentTurnOutput`: the validated model reply |
| `policy.py` | Pure stage/intent → action/next-step rules and post-action wording |
| `actions.py` | Runs allowlisted actions through the existing typed tools |
| `conversation.py` | Orchestration and persistence |
| `twiml.py` | TwiML builders (official SDK), absolute URLs only |

Stages: `IDENTITY_CHECK` ("Hello, may I speak with {customer_name}?") → `CONVENIENCE_CHECK`
(the Alex introduction plus the transcript notice) → `DISCUSSION` (the verified disclosure
and options question) or `CALLBACK_TIME` → `ENDED`. Payment details are spoken only after the
customer confirms identity and agrees to continue. Script values come from the database
(customer, installment, latest attempt, pending retry event), never from the model.

Classification: clear yes/no answers in the opening and any opt-out phrase are matched by
rules, so they need no model call. Everything else goes to GLM, which must return:

```json
{"spoken_response": "...", "intent": "REQUEST_PAYMENT_LINK", "resolution_preference": "PAYMENT_LINK",
 "requires_backend_action": true, "backend_action": "request_payment_link",
 "needs_human_escalation": false, "end_call": false, "callback_time": null}
```

Unknown intents or actions fail validation. One re-ask is made within the time budget, then a
safe fallback is used. **The policy, not the model, decides actions:** an action runs only when
the validated intent maps to it and the stage allows it, and a mismatched `backend_action` is
ignored. Spoken confirmations ("I've created a secure payment link…") are written by the
backend after the tool reports the real result. Model wording is filtered for links, long
digit runs, credential requests and unverified success claims.

| Intent | Backend behaviour | Real or simulated |
|---|---|---|
| REQUEST_PAYMENT_LINK | `request_payment_link` tool | simulated link; not delivered anywhere |
| RETRY_PAYMENT | reports the scheduled automatic retry | read-only; retries are clock-driven |
| ALREADY_PAID | note + event; "still shows as unpaid" | status check is a recorded request |
| DISPUTE_PAYMENT | support note; persuasion stops; call ends | recorded only |
| REQUEST_CALLBACK | `schedule_callback` (validated time) | real record |
| REQUEST_HUMAN_SUPPORT | support note; honest "can't transfer" | no human queue yet |
| DO_NOT_CALL | `mark_do_not_contact`; case `opted_out` | real, one-way |
| IDENTITY_NOT_CONFIRMED / DECLINE | polite end, nothing disclosed | — |
| UNCLEAR_RESPONSE | one clarification, then polite end | — |

Scenario behaviour: a paid installment (`SUCCESSFUL_PAYMENT` / `ALREADY_PAID`, or paid mid-call)
gets no recovery conversation. `BANK_TIMEOUT` (pending reconciliation) is never described as
failed. An unknown reason is described as "being checked".

Limits and failures: silence reprompts (`VOICE_MAX_SILENCE_PROMPTS`), turn and duration caps,
and a GLM budget (`VOICE_LLM_TIMEOUT_SECONDS`). Twilio drops slow webhooks (the trial sandbox
after ~5 s, observed live), so each turn is decided in an owned background task: if it is not
done within `VOICE_WEBHOOK_REPLY_WITHIN_SECONDS` the webhook returns `<Pause 1>` + `<Redirect>`
for the same turn, and the redirected request replays the stored reply once it exists. One
fallback is allowed before the call ends politely.

## Transcripts and audit

Per turn N: customer row `sequence 2N-1`, agent row `2N`, greeting `0`, all in
`conversation_turn`, with a unique `idempotency_key`, linked call, case and customer, UTC
`received_at` / `processed_at`, simulation `virtual_time`, `intent`, `resolution_preference`,
`backend_action` / `action_status` / `action_result`, `processing_status`, `error_category`.
For agent rows, `delivery_status` is `returned` (TwiML handed to Twilio) or `played` (Twilio's
next callback implies the prompt ran). A reply is never claimed as heard.

1. The customer turn is stored as `received` before the model is called, so a timeout never
   loses it.
2. The model and actions run with no transaction open.
3. The agent turn, the turn results and the call events commit together **before** TwiML is
   returned. If that write fails, the caller hears a goodbye, the customer turn is marked
   `failed`, and no agent turn is stored.

Duplicate deliveries replay the stored reply. A duplicate arriving mid-processing gets
`<Pause/><Redirect/>` and replays once the reply exists.

`call_event` (append-only, idempotent by key) records CALL_APPROVED, CALL_INITIATION_REQUESTED,
CALL_INITIATED / CALL_INITIATION_UNKNOWN / CALL_FAILED, CALL_RINGING, CALL_ANSWERED,
CALL_COMPLETED, CALL_BUSY, CALL_NO_ANSWER, CONVERSATION_STARTED, CONVERSATION_TURN_RECEIVED,
AGENT_RESPONSE_GENERATED, LLM_FALLBACK_USED, BACKEND_ACTION_*, CUSTOMER_* and
CONVERSATION_ENDED. Late or duplicate Twilio callbacks are recorded once and never regress a
terminal call status.

### Endpoints (operator `X-API-Key` required)

| Endpoint | Returns |
|---|---|
| `GET /api/v1/calls` | paginated summaries; filters `customer_id`, `recovery_case_id`, `status`, `created_from`, `created_to`, `scenario_id`, `simulation_id` |
| `GET /api/v1/calls/{id}/transcript` | ordered turns (sanitized text, intent, action, statuses) |
| `GET /api/v1/calls/{id}/audit` | call events and tool executions |
| `GET /api/v1/calls/{id}/transcript/export` | JSON attachment (transcript + audit) |

Transcript access and export are written to `audit_log`. There is one operator role and no
customer-facing access.

### Privacy, consent and retention

- **Text only; audio recording is not enabled.**
- Redaction (`src/common/redaction.py`) removes OTP/PIN/CVV/password values, card-like
  numbers, other long numbers, phone numbers and emails, on write and again on read/export.
  `original_text` is stored only when `TRANSCRIPT_STORE_ORIGINAL=true`, even then without
  secrets, and is never returned by an API.
- Customer speech before the transcript notice (the identity answer) is stored as
  `[not retained: before transcript notice]`. `TRANSCRIPT_POLICY=metadata_only` keeps no
  customer text at all. `call_session.transcript_consent` records `disclosed` or
  `not_retained`.
- Logs carry IDs, intents and character counts, never transcript text or webhook bodies.
- Retention: `uv run python -m scripts.manage purge-transcripts [--dry-run]` deletes the turns of
  calls that ended more than `TRANSCRIPT_RETENTION_DAYS` ago and sets `transcript_purged_at`.
  Call sessions, events (metadata only) and payment and recovery records are kept. Run it on a
  schedule (Task Scheduler / cron).
- **Needs product/legal confirmation:** whether a disclosure is sufficient or explicit opt-in
  consent is required before retaining text, the retention period, and any jurisdiction-specific
  call-recording or telemarketing rules. The defaults here are placeholders, not legal advice.
