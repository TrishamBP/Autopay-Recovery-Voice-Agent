---
paths:
  - "src/calls/**"
  - "src/voice/**"
  - "src/llm/**"
  - "src/tools/**"
  - "src/webhooks/**"
  - "src/websocket/**"
  - "tests/**/test_*call*.py"
  - "tests/**/test_*voice*.py"
  - "tests/**/test_*llm*.py"
  - "tests/**/test_*tool*.py"
  - "tests/**/test_*webhook*.py"
  - "tests/**/test_*websocket*.py"
  - "tests/**/test_*stream*.py"
---

# Voice agent: real-time transport and conversation

## Call lifecycle (keep these stages separate)

1. **Initiate** (`src/calls/`): an authenticated API request runs the policy checks from `security.md`, then creates a `call_session` row, and only then makes the Twilio REST call through an adapter. Retries are idempotent; never place a second call because a response timed out. Reconcile instead.
2. **Webhook** (`src/webhooks/`): the signed Twilio request returns TwiML `<Connect><Stream url="wss://…">` with `<Parameter>` values (session ID, single-use stream token). Status callbacks update `call_session`. This handler is short-lived and holds no conversation state.
3. **Stream** (`src/websocket/`): the long-lived Media Streams WebSocket. It authenticates the handshake, then hands frames to a per-call `voice` session.
4. **Pipeline** (`src/voice/`): STT → turn manager → LLM → tools → TTS → outbound audio.
5. **Teardown**: on `stop`, disconnect, timeout, or limit reached, cancel the per-call task group, drain or close queues, close provider sessions, and persist the final outcome in a fresh short transaction.

## Twilio Media Streams protocol

Follow Twilio's current Media Streams documentation. Check it before implementing, and **don't invent message types or fields**. As documented at the time of writing:
- Bidirectional streams use `<Connect><Stream>`. Messages from Twilio include `connected`, `start` (carries `streamSid`, `callSid`, `mediaFormat`, `customParameters`), `media` (base64 `payload`), `mark`, `dtmf`, and `stop`.
- Messages to Twilio are `media` (base64 audio for the `streamSid`), `mark` (to learn when playback reached a point), and `clear` (flush buffered outbound audio, used for barge-in).
- Audio is `audio/x-mulaw`, 8000 Hz, mono. Convert to or from provider formats in one place in `src/voice/`.

## WebSocket handling

- The handler stays thin: authenticate, parse JSON into Pydantic models (a discriminated union on `event`), and route to the session. All orchestration lives in `src/voice/` and is testable without a socket.
- Validate every frame: well-formed JSON, a known `event`, required fields, a `streamSid` that matches the session, base64 payload size within limits. Malformed frames are counted and logged without payload, and the connection closes past a threshold.
- Ignore or reject `media` that arrives before `start`.
- Run separate receive and send loops inside one `TaskGroup` per call. Use bounded `asyncio.Queue`s between stages with an explicit overflow policy (drop the oldest inbound frames, never block the receive loop forever). There is no unbounded audio buffer anywhere.
- Watchdogs: a stalled-stream timeout (no frames), a silence timeout, max call duration, and max turns. Each one triggers a graceful goodbye and then teardown.
- Session state is keyed per call and never module-global. Two simultaneous calls share nothing mutable.
- Track `call_sid`, `stream_sid`, `customer_id`, `turn_id`, and `correlation_id` in log context. Never log raw audio, payloads, secrets, or full transcripts.

## Turn management

- Each call runs **one response generation at a time**. A new user utterance during generation triggers barge-in: send `clear`, cancel the in-flight LLM and TTS tasks (cancellation must propagate), mark the turn interrupted, and start a new turn.
- Use `mark` events to know what the caller actually heard, and record only delivered text as said.
- Silence and unclear speech get a short reprompt, up to N times (from settings), and then a polite close with a recorded outcome.

## Conversation policy (enforced in code where possible)

- The opening discloses that this is an automated assistant calling on behalf of the service. Confirm you're speaking to the right person before giving account details. For a wrong person, disclose nothing and record `wrong_person`.
- State only facts returned by tools from the database. Never guess amounts, dates, reasons, or retry times.
- Use history and notes only when they are relevant to the customer's question. Don't recite them.
- Speak in short, natural phone sentences in the customer's `preferred_language` when it is supported. Ask one question at a time.
- Handle these situations: objections, refusal (accept it after one factual statement), callbacks, payment-link requests, already-paid claims (log for verification and never confirm payment), disputes and arrears (escalate), and opt-out (confirm, stop immediately, call `mark_do_not_contact`).
- Never ask for card numbers, CVV, OTP, PINs, or passwords. Never read URLs aloud. Never claim a payment succeeded.
- Never make threats, promise discounts, or invent plan changes.

## LLM integration

- `src/llm/` exposes a provider-neutral interface (for example `generate(messages, tools, *, timeout) -> stream of events`). Fireworks is one adapter behind it. Business logic never imports the Fireworks client.
- **Check the selected Fireworks model's documentation** for streaming, tool/function-calling support, context length, and the request format before relying on them. Record the findings next to the adapter.
- Prompt assembly is a pure function: system policy, then the call context the backend fetched, then conversation turns. Notes and transcripts go inside clearly delimited data blocks labelled untrusted and are never concatenated into instructions.
- Model calls use per-call timeouts and a bounded retry, and only before any audio from that response has been played. On failure, play a fixed fallback line ("I'm having trouble, I'll arrange a callback"), record the outcome, and end gracefully.
- Keep prompts in versioned files or constants, and unit-test the rendered prompts.

## Tools

- Each tool is a typed Pydantic argument model plus a handler that calls a domain service. Names match the data dictionary's `agent_tool_action` enum.
- The dispatcher **injects `customer_id`, `call_id`, and session from server-side context**. Arguments the LLM supplies for those are ignored or rejected. Validate everything else server-side (enums, future callback times inside calling hours, note length and secret patterns, the one-way DNC change).
- Side-effecting tools (send link, schedule callback, add note, mark DNC, log claim, record outcome) are idempotent per `(call_id, tool, key)`. Record each execution in `tool_executions`.
- Tool results returned to the LLM are minimal and contain no secrets (no link URLs, no phone numbers).
- Unknown tool names, invalid arguments, and authorization failures return a structured error to the model and are logged. They are never executed.
