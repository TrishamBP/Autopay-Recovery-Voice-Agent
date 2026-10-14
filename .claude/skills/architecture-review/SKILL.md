---
name: architecture-review
description: Broad design review of a module, feature or the whole service - modularity and boundaries, async correctness, reliability and failure modes, security, database transactions, observability and testability. Use before large merges, after major features, or when asked to assess the design.
argument-hint: "[module, feature, or 'repo']"
---

# Architecture review

## When to use

Use this before merging a large feature, after a milestone (foundation, Twilio, streaming, LLM), or when the user asks "is this design sound?". For line-level async or streaming audits, delegate to `async-review` or `websocket-review` and summarise their results here.

## Inputs and prerequisites

- Scope: `$ARGUMENTS` (a module, a feature, or the whole repo). Ask if it's unclear.
- Baseline: `.claude/CLAUDE.md` and all `.claude/rules/*.md`. Read the rules explicitly, since path-scoped ones may not be loaded.
- The current code, tests, `pyproject.toml`, the migrations, and the README.

## Workflow

1. **Inventory.** Map the modules, their dependencies (imports between domains), entry points, background tasks, and external providers. Note what exists against the target layout in `architecture.md`.
2. **Modularity.** Check ownership against the module table, look for cross-domain internals leakage and circular imports, check that handlers stay thin, and that API schemas are separate from ORM models. Flag both missing seams and needless abstraction.
3. **Async correctness.** Do a summary pass: lifespan-managed clients, timeouts, bounded concurrency, owned tasks, cancellation. Run `async-review` for depth.
4. **Reliability and failure modes.** For each external dependency (Twilio REST, webhooks, Media Streams, STT, LLM, TTS, DB), ask what happens when it is slow, down, returns garbage, or succeeds but the response is lost. Check retries, idempotency, reconciliation, and graceful fallbacks. Check shutdown with calls in flight.
5. **Security.** Check signature validation with the right URL, WebSocket auth, the call allowlist, DNC, and limits enforced in code, secrets handling, untrusted-text handling, log redaction, and safe error bodies.
6. **Database.** Check transaction scope (none spanning network awaits), session-per-unit-of-work, constraints and indexes, the migration history, idempotency keys, UTC timestamps, and SQLite/Postgres portability.
7. **Observability.** Check that correlation, call, stream, and turn IDs are in logs, that there is a structured format with no PII or audio, and that a failed call can be diagnosed from logs alone.
8. **Testability.** Check that provider fakes are injectable, the clock is injectable, test DBs are isolated, and that the critical paths in `testing.md` are covered. Run the quality gate if it's configured.
9. **Operational limits.** Check max concurrent calls, per-call resource use, and what saturates first.
10. **Report.**

## Expected outcomes checklist

- [ ] A dependency or module map, including deviations from the target architecture
- [ ] A failure-mode table: dependency → failure → current behaviour → risk → recommendation
- [ ] Findings ranked by severity with `file:line` evidence
- [ ] Each recommendation is the smallest change that fixes the risk, with its trade-off stated
- [ ] Quality-gate results, either the real output or "not run" with the reason

## Common failure modes

- Recommending a big re-architecture when a local fix suffices.
- Praising patterns that exist only in the rules but not in the code. Verify in the code.
- Ignoring the "succeeded but the response was lost" case for side effects.
- Treating prompt instructions as a security control.
- Reviewing only the happy path in the streaming lifecycle.

## Reporting

Start with a short verdict and the top three risks, then the detailed findings. Separate confirmed issues (with evidence) from concerns and open questions. Say what you didn't review. Never state that checks or tests passed unless you ran them in this session.
