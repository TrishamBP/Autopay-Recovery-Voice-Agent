---
name: async-review
description: Focused review of Python async code for event-loop blocking, missing await, unsafe task creation, swallowed cancellation, missing timeouts, client lifecycle leaks, unbounded concurrency and CPU-bound work. Use on a diff, file, or module in this FastAPI service.
argument-hint: "[path, module, or 'diff']"
---

# Async review

## When to use

Use this after writing or changing any `async def` code, before merging, or when you suspect latency spikes, hangs, leaked tasks, or "event loop is blocked" symptoms. It is narrower than `architecture-review`.

## Inputs and prerequisites

- Target: `$ARGUMENTS` (a path or module), or the current diff (`git diff`, or the files the user names). If there's no target, ask.
- The baseline is `.claude/rules/fastapi-async.md`.

## Workflow

1. **Scope.** List the files in scope, and read them in full along with the callers they depend on.
2. **Mechanical scan** (use Grep, then confirm each hit by reading it):
   - Blocking calls: `time\.sleep`, `requests\.`, `urllib`, `open\(` on large files, `openpyxl`, `pandas`, `subprocess\.run`, a sync `Session`, `twilio.rest.Client` methods called directly.
   - Tasks: `create_task`, `ensure_future`, `BackgroundTasks` (is every task owned, referenced, and cancelled?).
   - Cancellation: `except BaseException`, `except asyncio.CancelledError`, bare `except:`. Does each one re-raise?
   - Clients: `AsyncClient(`, `create_async_engine(` outside lifespan or `database.py`.
   - Loops: `asyncio.run(`, `get_event_loop(`, `run_until_complete`.
   - Queues: `Queue()` without `maxsize`.
3. **Semantic review** of each async function:
   - Every awaitable is awaited, and the return values of coroutines are not discarded.
   - Each external call has a timeout, either a client-level one or `asyncio.timeout`.
   - Concurrency is bounded (Semaphore, bounded Queue, TaskGroup).
   - Cleanup runs in `finally` or `async with`, survives cancellation, and is itself time-bounded.
   - No DB transaction stays open across a network await.
   - Retries are bounded, use backoff, and only cover idempotent operations.
   - CPU-heavy work is offloaded (process pool or offline script), and sync SDKs run through `run_in_threadpool`/`to_thread`.
   - No `async def` without awaitable work, unless an interface requires it.
4. **Verify with tests where feasible.** Check whether timeout and cancellation tests exist, and propose or write the missing ones if asked.
5. **Report.**

## Expected outcomes checklist

- [ ] Every finding has `file:line`, severity (blocker / major / minor), what goes wrong at runtime, and a concrete fix
- [ ] Pattern hits from the grep were confirmed by reading the code, with false positives discarded
- [ ] The test gaps for timeouts, cancellation, and limits are listed

## Common failure modes (of the review itself)

- Flagging `open()` on a tiny config file as blocking. Judge the actual cost.
- Missing a blocking call hidden inside a sync helper that is called from async code. Follow the call chain.
- Missing that `except Exception` around `await` is fine but `except BaseException` isn't.
- Assuming a library is async because its method names look async. Check it.

## Reporting

Group findings by severity. If you find nothing, say "no issues found in <scope>" and list what you checked. Don't pad the report with speculative findings. Mark uncertain items as questions. If you ran tests, include the command and the real result; otherwise state that the review was static.
