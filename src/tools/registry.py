"""Fixed registry of typed tools the LLM may select from."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.enums import ActorType
from src.common.time import Clock, utc_now
from src.config import Settings
from src.tools.schemas import ToolArgs


@dataclass(frozen=True)
class ToolContext:
    """Bound by the backend from the authenticated call/operator session, never by the LLM."""

    customer_id: str
    actor_type: ActorType
    call_session_id: str | None = None


@dataclass(frozen=True)
class ToolRuntime:
    settings: Settings
    clock: Clock = utc_now


Handler = Callable[[AsyncSession, ToolContext, ToolArgs, ToolRuntime], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    handler: Handler
    side_effecting: bool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def llm_tool_definitions(self) -> list[dict[str, object]]:
        """OpenAI-compatible function definitions (for a model that supports tool calling)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.args_model.model_json_schema(),
                },
            }
            for spec in self._tools.values()
        ]
