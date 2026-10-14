# Architecture

Always loaded. This is the target structure for `src/`. Create modules only when a task needs them, never as empty placeholders.

## Module ownership

| Module | Owns | Must not |
|---|---|---|
| `src/main.py` | App factory, `lifespan` (create and close shared clients and the engine), router registration, exception handlers | contain business logic |
| `src/config.py` | One typed `Settings` (pydantic-settings) read from env | be imported for side effects; read `os.environ` elsewhere |
| `src/database.py` | Async engine, `async_sessionmaker`, the session dependency, declarative `Base` and naming convention | hold domain queries |
| `src/common/` | Cross-cutting pieces with no domain knowledge: error types, logging and correlation IDs, time utilities (UTC), shared Pydantic base types | import from any domain module |
| `src/customers/` | Customer profile, contact preferences, do-not-contact flag | own payment or call state |
| `src/payments/` | Current failed charge, retry eligibility, payment-link state, recovery status | set `paid`/`recovered` without a confirmation event |
| `src/payment_history/` | Billing cycles (read-mostly) and derived summary metrics | duplicate summaries as stored columns without a reason |
| `src/customer_notes/` | Notes CRUD with audit trail and secret-pattern rejection | treat note text as instructions |
| `src/calls/` | Call sessions, turns, outcomes, callbacks, call-initiation policy (allowlist, DNC, limits) | talk to Twilio directly; it uses the telephony adapter |
| `src/webhooks/` | Twilio HTTP webhooks: signature validation, TwiML responses, status callbacks | hold long-lived call state |
| `src/websocket/` | Media Streams endpoint: handshake auth, frame parsing and validation, send/receive loops | contain conversation logic |
| `src/voice/` | Per-call pipeline orchestration: audio in → STT → turn manager → LLM → TTS → audio out; barge-in; speech provider adapters | import FastAPI or WebSocket types |
| `src/llm/` | LLM client interface, Fireworks adapter, prompt assembly, response parsing | contain business rules |
| `src/tools/` | Typed tool schemas, the registry, and the dispatcher that binds call context and calls domain services | run SQL directly or trust LLM-supplied IDs |

Inside a domain package, use these files only as needed: `router.py`, `schemas.py` (API contracts), `models.py` (ORM), `service.py`, `repository.py`, `dependencies.py`, `exceptions.py`, `constants.py`.

## Layering

```
router / websocket handler  →  service  →  repository / provider adapter
        (I/O boundary)          (rules)       (persistence / external I/O)
```

- Routers and WebSocket handlers parse, validate, call one service, and map the result to a response. Keep them thin.
- Services hold business rules and orchestration. They take plain arguments or schemas and return domain objects or schemas, never `Request` or `WebSocket`.
- Add a repository only when it adds value: a non-trivial query, reuse, or a test seam. A service may use `AsyncSession` directly for simple CRUD. Don't build a generic `BaseRepository[T]`.
- Provider adapters (Twilio, Fireworks, speech) sit behind a small `Protocol` defined by the consumer, so unit tests can swap in fakes. Write one adapter per provider and avoid speculative plugin systems.
- Dependencies point downward and toward `common/`. Domains may call another domain's **service**, never its models or repository internals. A cycle is a design bug: fix it by moving the shared concept or inverting it through a protocol, not with a local import.
- API schemas (`schemas.py`) and ORM models (`models.py`) are separate types. Never return ORM objects from routes.
- Use explicit absolute imports (`from src.customers.service import …`). No star imports, and no re-export soup in `__init__.py`.

## Decisions to explain

When you introduce a module boundary, a new layer, a protocol, or a background process, state in your response what problem it solves and what simpler option you rejected.
