---
name: websocket-review
description: Focused review of the Twilio Media Streams / WebSocket implementation - protocol correctness, handshake auth, message validation, backpressure, bounded buffering, single active turn, barge-in, disconnect cleanup and per-call session isolation.
argument-hint: "[path or 'diff']"
---

# WebSocket / Media Streams review

## When to use

Use this when code in `src/websocket/`, `src/voice/`, or the TwiML-producing webhooks changes, or when debugging dropped audio, stuck calls, leaked tasks, crosstalk between calls, or interruption bugs.

## Inputs and prerequisites

- Target: `$ARGUMENTS` or the current diff. Read the handler, the per-call session, the queues, the provider adapters, and the related tests.
- The baseline is `.claude/rules/voice-agent.md` (the transport and turn sections) and `.claude/rules/fastapi-async.md`.
- Open Twilio's current Media Streams documentation (with WebFetch if available) to check message shapes. Don't rely on memory alone. If you can't reach the docs, say so in the report.

## Workflow

1. **Map the lifecycle.** Trace connect → handshake auth → `connected` → `start` → `media`* → `stop`/disconnect → teardown, and draw the tasks and queues involved.
2. **Protocol.** Compare the inbound and outbound message models with the Twilio docs: event names, `streamSid` on outbound messages, base64 mu-law 8 kHz mono, `mark` and `clear` usage. Flag any invented field or event.
3. **Authentication.** Confirm the handshake is verified (signature or a single-use token bound to the call SID) before any processing, and that the token can't be replayed.
4. **Validation.** Check malformed JSON, unknown events, `media` before `start`, a mismatched `streamSid`, oversized payloads, and the policy that closes the connection past a malformed-frame threshold.
5. **Backpressure.** Every queue is bounded, with a documented overflow policy. The receive loop can't be blocked by slow STT, LLM, or TTS. Generated audio isn't buffered without limit.
6. **Turns.** Only one generation runs at a time. Barge-in sends `clear` and cancels the LLM and TTS tasks with cancellation propagating. Marks track what was actually played.
7. **Timeouts and limits.** Check the stalled-stream, silence, max-duration, and max-turns timeouts, and the provider timeouts, each with a graceful close.
8. **Teardown.** On every exit path (normal stop, client disconnect, exception, server shutdown), tasks are cancelled and awaited, queues are released, provider sessions are closed, and the final state is persisted in a fresh short transaction. Verify there are no awaits on a closed socket.
9. **Isolation.** No module-level mutable state; each session is keyed by call. Logs carry call and stream IDs and no audio or PII.
10. **Tests.** Confirm tests exist for each item above, including two concurrent sessions.

## Expected outcomes checklist

- [ ] The lifecycle diagram or description matches the code
- [ ] Protocol deviations from the Twilio docs are listed, citing the doc section
- [ ] Every queue and buffer has a bound and an overflow behaviour
- [ ] Every exit path cleans up tasks, queues, and provider sessions
- [ ] Session isolation is verified, and the concurrency tests exist or are listed as gaps

## Common failure modes

- `receive_text()` in a loop with no timeout, so a stalled stream hangs forever.
- `send_*` called from multiple tasks without a single writer, which interleaves frames.
- Barge-in that stops TTS but leaves the LLM stream running, or that forgets `clear`, so stale audio keeps playing.
- Catching `WebSocketDisconnect` in the receive loop while the send loop keeps running.
- A DB transaction opened at `start` and held for the whole call.

## Reporting

For each finding, give `file:line`, severity, the failure scenario (the event sequence that triggers it), and the fix. State which checks were static and which were exercised by running tests, and include the real test output. Say explicitly if the Twilio docs couldn't be consulted.
