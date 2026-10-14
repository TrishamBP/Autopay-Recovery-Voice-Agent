"""Shared FastAPI dependencies."""

import secrets
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.exceptions import AuthenticationError, ServiceUnavailableError
from src.config import Settings
from src.database import get_session


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_operator(
    settings: SettingsDep, x_api_key: Annotated[str | None, Header()] = None
) -> None:
    """Operator authentication: constant-time comparison against OPERATOR_API_KEY."""
    expected = settings.operator_api_key.get_secret_value() if settings.operator_api_key else ""
    if not expected:
        raise ServiceUnavailableError("Operator API is disabled: OPERATOR_API_KEY is not set")
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise AuthenticationError("Missing or invalid X-API-Key")


OperatorDep = Depends(require_operator)
