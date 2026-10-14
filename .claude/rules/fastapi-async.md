---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---

# FastAPI and async rules

## When to use `async def`

- Endpoints, dependencies, services, repository methods, provider adapters, and WebSocket handlers that do I/O are `async def` and `await` async libraries: `AsyncSession`, `httpx.AsyncClient`, WebSocket `send_*`/`receive_*`.
- A function with no awaitable work stays a plain `def`. Don't add `async` for appearance. A sync `def` *endpoint* runs in FastAPI's threadpool. That is acceptable for genuinely sync work, but it is not the default here.
- Every coroutine call is awaited. An un-awaited coroutine is a bug (Ruff's `ASYNC` rules and pytest warnings help catch this).

## Never block the event loop

These are forbidden inside `async def`:
- `time.sleep` (use `asyncio.sleep`)
- `requests` or any sync HTTP client
- sync DB drivers or sync `Session`
- sync file I/O on large files
- `openpyxl`/`pandas` workbook parsing
- CPU-heavy loops
- the sync Twilio REST client called directly

- If a sync SDK is unavoidable (for example the Twilio REST client), wrap the call with `await starlette.concurrency.run_in_threadpool(fn, ...)` or `asyncio.to_thread`, inside the adapter, so callers see an async API.
- Substantial CPU-bound work (bulk audio transcoding, large data transforms) goes to a `ProcessPoolExecutor` or an offline script, never the request or call path. `asyncio` adds I/O concurrency; it doesn't make CPU-bound Python faster.
- Workbook import is an offline `scripts/` job, not an HTTP endpoint.
- Never call `asyncio.run()` from code that may already be inside a running loop.

## Client and resource lifecycle

- Create long-lived resources **once** in the FastAPI `lifespan`: the DB engine, `httpx.AsyncClient`, and provider clients. Store them on `app.state`, expose them through dependencies, and close them on shutdown (`await client.aclose()`, `await engine.dispose()`).
- Never create an engine or HTTP client per request or per call turn.
- Every external client has explicit timeouts (`httpx.Timeout(connect=…, read=…, write=…, pool=…)`) and connection limits (`httpx.Limits`). Values come from settings, and there are no infinite defaults.

## Concurrency

- Use `asyncio.TaskGroup` for structured fan-out, and `asyncio.timeout()` for deadlines.
- Every `create_task` is owned. Keep a reference, cancel it on teardown, and observe its exception. Fire-and-forget tasks are forbidden. Per-call tasks belong to the call session's `TaskGroup`.
- Bound concurrency with `asyncio.Semaphore` (provider calls, concurrent calls) and bounded `asyncio.Queue(maxsize=…)`. Never use an unbounded queue or an unbounded task spawn loop.
- FastAPI `BackgroundTasks` only suits short, non-critical post-response work. Durable or retryable work needs a real job mechanism, so ask first.

## Cancellation

- Never swallow `asyncio.CancelledError`. If you catch it to clean up, re-raise it. `except Exception` doesn't catch it on 3.8+, so keep it that way and don't use `except BaseException` without re-raising.
- Cleanup goes in `finally` or `async with`, and it must itself be bounded (`asyncio.timeout`) so shutdown can't hang.

## Errors, retries and observability

- Map domain exceptions to HTTP responses in registered exception handlers, not with ad-hoc `try/except` in every route.
- Retry only idempotent operations, with a bounded count, exponential backoff, and jitter. **Never blindly retry a side effect whose outcome is unknown** (placing a call, sending a link). Use an idempotency key or reconcile instead.
- Logs are structured (key-value or JSON) and carry `correlation_id`, plus `call_sid`, `stream_sid`, `customer_id`, and `turn_id` when known. Propagate them with `contextvars`, set by middleware or the call session.
- Pass log values as fields, not by formatting untrusted strings into the message.
