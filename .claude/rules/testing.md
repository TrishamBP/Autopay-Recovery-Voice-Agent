---
paths:
  - "tests/**"
  - "conftest.py"
  - "pyproject.toml"
---

# Testing

## Stack and layout

- Use pytest with pytest-asyncio. Prefer setting `asyncio_mode = "auto"` in `pyproject.toml`, so async tests need no per-test marker. Choose this once when configuring and keep it.
- Use `tests/unit/` for pure logic and services with fakes, `tests/integration/` for the real app plus a real (isolated) DB, and `tests/live/` for real providers, which is **opt-in**.
- HTTP tests call the app in-process with `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")`.
  - `ASGITransport` doesn't run lifespan events. Enter the app's lifespan explicitly in the fixture, or override the dependencies that need lifespan resources.
- WebSocket tests use Starlette's `TestClient.websocket_connect` (sync) or a driven fake transport at the service layer. Keep most protocol logic testable without a socket.

## Isolation and determinism

- Each test gets a fresh database: a temp-file SQLite, or a transaction-rolled-back Postgres schema. Never use a developer's DB.
- Seed from deterministic fixtures derived from the synthetic dataset (a small subset is fine). Don't read the real workbook in unit tests.
- Freeze or inject time. Business logic gets "now" through a clock dependency, so tests never depend on wall-clock time or on `sleep`.
- Override settings in tests through dependency overrides or constructor arguments, never by mutating `os.environ` globally.

## Providers

- Unit and integration tests mock Twilio, Fireworks, and speech providers with fakes that implement the same `Protocol`. Ordinary runs make no network calls, need no credentials, and dial no phones. A missing credential must never make a normal test fail or skip silently.
- Live tests carry `@pytest.mark.live`, are deselected by default (`addopts = "-m 'not live'"`), require explicit env vars, and may only target allowlisted test numbers.

## What must be tested

- HTTP: validation errors, auth failures, Twilio signature accepted/rejected, safe error bodies.
- Async behaviour: timeouts, cancellation propagating (not swallowed), cleanup on cancel, semaphore and queue limits respected.
- WebSocket: malformed JSON, unknown event, out-of-order events (`media` before `start`), wrong `streamSid`, oversized or invalid payload, abrupt disconnect, stalled stream, cleanup of tasks and queues afterwards, two concurrent calls with no state bleed.
- Tools: argument validation, customer binding from call context (LLM-supplied IDs ignored or rejected), idempotency (calling twice gives one effect), opt-out and DNC enforcement, `payment_status` never set by a tool.
- Conversation flows: see the `voice-agent-testing` skill.

## Reporting

Run the tests before claiming they pass. Report the exact command and the pass/fail/skip counts. If you couldn't run something, say so and say why.
