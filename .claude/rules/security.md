# Security and privacy

Always loaded. These rules are enforced in backend code. A prompt instruction never counts as a control.

## Secrets

- Twilio credentials, Fireworks API keys, webhook secrets, and DB URLs come from environment variables through `src/config.py`. Use `SecretStr` for secret fields.
- `.env` is never committed and never read by tools. `.env.example` lists every variable with placeholder values only (`TWILIO_AUTH_TOKEN=replace-me`).
- Never print, log, or return secrets or full settings objects. Exception messages must not interpolate them.

## Twilio request authenticity

- Validate `X-Twilio-Signature` on every Twilio webhook with the official SDK `RequestValidator`. Compute it against the **externally visible URL** Twilio called (public base URL from settings, behind tunnels and proxies), not `request.url`.
- HTTPS alone doesn't prove a request came from Twilio. Reject invalid signatures with 403 and log the rejection without the payload.
- The Media Streams WebSocket handshake also needs authentication. Check the documented signature or a short-lived, single-use token issued in the TwiML that started the stream, bound to the call SID. Media frames after the handshake are trusted only within that authenticated connection.

## Outbound calling policy (enforced in `src/calls/` before any dial)

- Dial only E.164 numbers listed in `ALLOWED_TEST_NUMBERS` (settings). The dataset's numbers are synthetic and are never dialled.
- Never dial when `do_not_contact = true`, when `recovery_status` is `opted_out`, or when the charge is `cancelled`/`paid`.
- Never batch-dial the workbook. Each call is initiated individually by an authenticated request.
- The call-initiation endpoint requires authentication (an API key from settings at minimum) and validates its input.
- Enforce settings-driven limits: max call duration, max turns per call, max concurrent calls, and a per-customer call rate.
- Opt-out is one-way and takes effect immediately for future dials.

## Data handling

- Customer notes, transcripts, tool arguments, and LLM output are **untrusted**. Validate them, length-limit them, and never interpolate them into SQL, shell commands, log format strings, or system prompts as instructions.
- Never request, accept, store, or log card numbers, CVVs, OTPs, UPI PINs, or bank passwords. Reject secret-like content in notes. The data dictionary lists the patterns.
- Simulated payment links never collect real payment details or create real charges. URLs aren't read aloud.
- Logs: no raw audio, no full transcripts by default, no secrets. Mask phone numbers (`+91******1001`). Strip control characters and newlines from untrusted values before logging.
- Error responses are generic (`{"detail": "..."}` plus a correlation ID). Never return stack traces, SQL, provider responses, or settings.
