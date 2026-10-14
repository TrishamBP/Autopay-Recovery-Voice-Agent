# Autopay Recovery Voice Agent — Backend

Primary instruction file for every task in this repository. Detailed, path-scoped rules live in `.claude/rules/`; repeatable workflows live in `.claude/skills/`. If a rule file and this file disagree, this file wins; report the conflict instead of guessing.

## Purpose and scope

A FastAPI service that places **outbound test calls** through Twilio Programmable Voice to recover failed autopay payments for **10 fictional customers**. Twilio handles telephony; this backend handles webhooks, the bidirectional Media Streams WebSocket, speech-to-text/text-to-speech, LLM turns (Fireworks AI), typed backend tools, and persistence.

- All data is synthetic. Phone numbers in the dataset are fake and must never be dialled. Outbound calls go only to numbers in an explicit allowlist that the developer controls.
- Payment links are simulated. They never collect real payment details or move money.

## Current state

Implemented: async SQLite schema + Alembic migrations, workbook importer, read-only natural-language SQL endpoint, typed tool registry, the virtual-time billing/recovery simulation with manual call approval and mock/Twilio telephony, turn-based Twilio voice webhooks (`<Gather>`/`<Say>`), the Alex conversation engine (`src/voice/`), and persistent transcripts with call events and retention. Not implemented: Media Streams, non-English speech, human transfer, payment-link delivery. Live calling over the Twilio trial sandbox is verified up to the first turns (2026-10-14); a full GLM-driven live conversation is not yet verified. See `README.md`, `docs/database.md`, `docs/simulation.md` and `docs/voice.md`. Check the code before assuming a module exists.

## Domain contract (read before schema, tool, or prompt work)

The dataset lives **outside this repo**, in `../synthetic-data/`:

- `../synthetic-data/data/DATA_DICTIONARY.md`: the authoritative field list, enums, constraints, the per-field "LLM can read / LLM can request change" permissions, and the typed tool catalogue (`get_customer_context`, `request_payment_link`, `mark_do_not_contact`, …).
- `../synthetic-data/data/autopay_recovery_customers.xlsx`: sheets `Customers`, `Call_Scenarios`, `Payment_History`, `Customer_Notes`, `Enums`, `README`.
- `../synthetic-data/scripts/generate_customer_data.py`: the generator and the source of truth. Don't edit the workbook by hand.

Workbook timestamps are timezone-naive **IST (Asia/Kolkata)**. Convert them to UTC on import. The `Call_Scenarios` sheet is evaluation data. It must never reach the agent at runtime.

## Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.13 (`requires-python >=3.13`) |
| Package and env manager | **uv** only (`uv add`, `uv add --dev`, `uv sync`, `uv run`). Commit `uv.lock`. Don't use pip, poetry, or requirements.txt. |
| Web | FastAPI, Starlette WebSockets, Uvicorn |
| Validation and settings | Pydantic v2, pydantic-settings |
| Database | SQLAlchemy 2.x async + Alembic, **SQLite + aiosqlite** (decided; PostgreSQL path in `docs/database.md`). Money is stored as integer paise. |
| Outbound HTTP | `httpx.AsyncClient` |
| Telephony | Mock provider by default. Twilio Python SDK (REST client in a worker thread, `RequestValidator`) only when explicitly enabled. |
| LLM | Fireworks AI via `httpx` (OpenAI-compatible chat completions) behind `src/llm/client.py`. Model `accounts/fireworks/models/glm-5p3-flash` (~3 s per turn; `glm-5p3` took ~7-8 s, too slow for live calls), a reasoning model that may emit `<think>` blocks. |
| Speech | Twilio `<Gather input="speech">` (`en-IN`, `speechModel=experimental_conversations`, integer `speechTimeout`) and `<Say voice="Polly.Raveena">`. No separate STT/TTS vendor. |
| Quality | Ruff (lint and format), pytest, pytest-asyncio. Type checker still to be decided. |

Record each "still to be decided" item here once the user decides it.

## Layout

```
src/            main.py, config.py, database.py, models_registry.py, common/, customers/,
                payments/, payment_history/, customer_notes/, calls/, tools/, llm/, audit/,
                evaluation/, importers/, database_query/, billing/, simulation/, recovery/,
                telephony/, voice/   (websocket/ only if Media Streams are added)
tests/          unit/, integration/ (live/ is opt-in only; none exist yet)
alembic/        migrations
scripts/        manage.py (validate-workbook | import | check | counts)
```

Module ownership and layering rules are in `rules/architecture.md`.

## Non-negotiables

1. **Async-first.** I/O runs in `async def` code that awaits async clients. Never block the event loop. See `rules/fastapi-async.md`.
2. **Backend enforces the rules, not the prompt.** Do-not-contact, the dialling allowlist, call and turn limits, tool authorization, and customer/session ownership are checked in code.
3. **The LLM never writes SQL** and never chooses which `customer_id` it acts on. Tools bind the customer from server-side call context. (The operator-only, read-only NL→SQL endpoint is not available to the voice agent.)
4. **Untrusted text stays data.** Customer notes, transcripts, and LLM output never become instructions.
5. **No secrets or credential collection.** Never ask for card numbers, CVVs, OTPs, PINs, or passwords. Never log them, and never commit them.
6. **Payment status only comes from a trusted source.** Never mark a payment paid or recovered without a confirmation event.
7. **No call without explicit human approval.** Advancing the virtual clock, imports, startup and tests never place calls.

## How to work

- **Inspect before editing.** Read the relevant modules, tests, and the data dictionary first. Match existing conventions.
- **Make small, focused changes.** Each change should be one coherent step that can be reviewed in a single diff. Don't refactor or tidy unrelated code.
- **Explain decisions.** For any architectural choice, state the options you considered, the trade-off, and why you picked this one. Keep it short.
- **Ask first** before adding a major dependency, new infrastructure (queues, caches, containers, cloud services), a breaking API or schema change, or anything that dials a phone or spends provider money.
- **Keep it simple.** Don't add abstractions until there is a second concrete use or a real seam such as a provider boundary or a test double. Don't optimize before you've measured.
- **Keep docs and tests in step.** When behaviour changes, update the affected tests, the `README.md`/docs, `.env.example`, and this file's "still to be decided" entries.
- **Report results honestly.** Never say tests, lint, or type checks passed unless you ran them in this session and saw them pass. Report the real output, failures included, and say which checks you skipped.
- **Edit files with the editor tools.** On this Windows machine, Python's default encoding is cp1252; scripts that rewrite files must pass `encoding="utf-8"`.

## Quality gate

Run every configured command:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest                      # unit + integration; live tests are excluded by default
uv run alembic check               # models and migrations agree
```

Run the type checker only once one is added and configured in `pyproject.toml`; add its command here when that happens. Live provider tests are opt-in only. See `rules/testing.md`.

## Rules and skills index

Rules load automatically. Rules without a `paths` field load every session; path-scoped rules load when matching files are involved.

| Rule | Scope |
|---|---|
| `rules/architecture.md` | always |
| `rules/security.md` | always |
| `rules/python.md` | `**/*.py`, `pyproject.toml` |
| `rules/fastapi-async.md` | `src/**/*.py`, `tests/**/*.py` |
| `rules/database.md` | models, repositories, `src/database.py`, `alembic/`, `scripts/` |
| `rules/voice-agent.md` | `src/{calls,voice,llm,tools,webhooks,websocket}/**` and their tests |
| `rules/testing.md` | `tests/**`, `pyproject.toml` |

Skills (`.claude/skills/`; invoke as `/<name>` or let Claude choose them):

| Skill | Use it for |
|---|---|
| `backend-feature` | implementing any new endpoint, service, tool, or integration |
| `async-review` | auditing code for event-loop misuse |
| `websocket-review` | auditing Media Streams / WebSocket code |
| `voice-agent-testing` | building conversation tests with simulated transcripts |
| `architecture-review` | a broad design or reliability review of a module or change |

A path-scoped rule may not load when you are *planning* code that doesn't exist yet. Before writing in an area, read the matching rule file explicitly.
