"""Structured JSON logging with a per-request correlation ID."""

import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_HEADER = b"x-request-id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
# Attributes present on every LogRecord; anything else came from `extra=` and is emitted.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # The Twilio SDK logs every API request (URL, headers) at INFO; keep only problems.
    logging.getLogger("twilio").setLevel(logging.WARNING)


class CorrelationIdMiddleware:
    """Accepts a well-formed incoming X-Request-ID or generates one; echoes it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers", [])).get(_HEADER, b"").decode("latin-1")
        correlation_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = correlation_id_var.set(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((_HEADER, correlation_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            correlation_id_var.reset(token)
