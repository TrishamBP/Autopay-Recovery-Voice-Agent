"""Provider-neutral LLM interface and the Fireworks AI adapter.

Fireworks exposes an OpenAI-compatible Chat Completions API (`POST {base}/chat/completions`).
We use only the plain request/response shape (model, messages, max_tokens, temperature ->
choices[0].message.content). No streaming, JSON mode or tool calling is assumed, because those
depend on the selected model; check the model's documentation before relying on them.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from src.config import Settings

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


class LLMError(Exception):
    """Base class. Messages are safe for logs (no prompts, keys or response bodies)."""


class LLMNotConfiguredError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMProviderError(LLMError):
    pass


class LLMMalformedResponseError(LLMError):
    pass


class LLMClient(Protocol):
    async def complete(
        self, messages: list[ChatMessage], *, max_tokens: int, temperature: float = 0.0
    ) -> str: ...


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    """Long-lived client (created in lifespan) with explicit timeouts and connection limits."""
    timeout = settings.llm_timeout_seconds
    return httpx.AsyncClient(
        base_url=settings.fireworks_base_url,
        timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


class FireworksClient:
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    async def complete(
        self, messages: list[ChatMessage], *, max_tokens: int, temperature: float = 0.0
    ) -> str:
        settings = self._settings
        if not settings.llm_configured or settings.fireworks_api_key is None:
            raise LLMNotConfiguredError("FIREWORKS_API_KEY and FIREWORKS_MODEL must be set")
        body = {
            "model": settings.fireworks_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {settings.fireworks_api_key.get_secret_value()}"}
        # Text generation has no side effects, so bounded retries with backoff are safe.
        for attempt in range(settings.llm_max_retries + 1):
            last = attempt == settings.llm_max_retries
            try:
                response = await self._http.post("/chat/completions", json=body, headers=headers)
            except httpx.TimeoutException as exc:
                if last:
                    raise LLMTimeoutError("LLM request timed out") from exc
            except httpx.TransportError as exc:
                if last:
                    raise LLMProviderError("LLM provider unreachable") from exc
            else:
                if response.status_code == 200:
                    return _extract_content(response)
                if response.status_code not in self._RETRYABLE_STATUS or last:
                    logger.warning("llm request failed", extra={"status": response.status_code})
                    raise LLMProviderError(f"LLM provider returned HTTP {response.status_code}")
            await asyncio.sleep(0.5 * 2**attempt + random.uniform(0, 0.25))  # noqa: S311  jitter
        raise LLMProviderError("LLM request failed")  # unreachable; keeps type checkers happy


def _extract_content(response: httpx.Response) -> str:
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMMalformedResponseError(
            "LLM response did not match the chat-completions shape"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMMalformedResponseError("LLM response content was empty")
    return content
