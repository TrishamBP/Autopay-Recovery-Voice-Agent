"""Identifier generation for backend-created rows."""

import uuid


def new_id() -> str:
    return str(uuid.uuid4())
