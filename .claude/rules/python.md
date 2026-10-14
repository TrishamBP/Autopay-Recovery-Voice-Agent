---
paths:
  - "**/*.py"
  - "pyproject.toml"
---

# Python conventions

## Tooling (uv only)

- Add dependencies with `uv add <pkg>` and dev tools with `uv add --dev <pkg>`. Run everything with `uv run <cmd>`. Commit `uv.lock`. Never hand-edit the lock or use pip.
- Ruff handles both linting and formatting. Its configuration lives in `pyproject.toml` and is the source of truth. Don't fight it with inline `# noqa` unless you add a reason (`# noqa: E501  long URL`).
- A new runtime dependency needs the user's approval (see CLAUDE.md). Prefer the standard library or an already-installed package.

## Style

- Type hints on every function signature and public attribute in `src/`. Use modern syntax (`list[str]`, `X | None`) and avoid `Any` unless it sits at a boundary and you note why.
- Names: `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE` for constants. Module names are singular nouns or roles (`service.py`, `schemas.py`).
- Use `StrEnum` for the data dictionary's enums, with values identical to the dictionary's.
- Money is `Decimal`, never `float`. In Pydantic use `condecimal(max_digits=10, decimal_places=2)` or an annotated equivalent.
- Time: use `datetime.now(UTC)`, never naive `datetime.now()` or `utcnow()`. IST conversion uses `zoneinfo.ZoneInfo("Asia/Kolkata")`, which needs the `tzdata` package on Windows.
- Use `pathlib` for paths. Use f-strings except in log calls, which take structured fields (see `fastapi-async.md`).
- Raise specific exceptions defined in the owning module's `exceptions.py`. Never use a bare `except:`. `except Exception` is allowed only at a top-level boundary that logs it and either re-raises or converts it.
- Keep functions small and single-purpose. Return early instead of nesting.
- Comments explain *why*, not *what*. Give public services and adapters a one-line docstring. Don't write essay docstrings.
- Prefer composition, plain functions, and `dataclass`/Pydantic models over class hierarchies. Use `Protocol` for provider seams.
- Don't keep mutable module-level state. Shared clients live on `app.state` and are created in `lifespan`.
