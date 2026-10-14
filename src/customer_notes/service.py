"""Customer-note CRUD.

Notes are untrusted text. Every write validates length and rejects secret-like content, records an
audit event, and rewrites the customer's notes digest (`customer.customer_notes`,
`customer.notes_updated_at`) in the SAME transaction so the cache cannot diverge.
Deletion is intentionally not implemented (destructive; needs explicit authorization later).
"""

import re
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import record_audit
from src.common.enums import ActorType, NoteCreatedBy, NoteType
from src.common.exceptions import NotFoundError, ValidationFailedError
from src.common.redaction import contains_secret_like_content
from src.common.time import Clock, utc_now
from src.customer_notes.models import NOTE_MAX_LENGTH, CustomerNote
from src.customers.models import Customer
from src.customers.service import get_customer_for_update

DIGEST_SEPARATOR = " | "
_NOTE_NUMBER = re.compile(r"^NOTE-(\d+)$")


def validate_note_text(text: str) -> str:
    text = text.strip()
    if not 1 <= len(text) <= NOTE_MAX_LENGTH:
        raise ValidationFailedError(f"note_text must be 1-{NOTE_MAX_LENGTH} characters")
    if contains_secret_like_content(text):
        raise ValidationFailedError(
            "note_text looks like it contains payment credentials or long digit sequences"
        )
    return text


async def list_notes(session: AsyncSession, customer_id: str) -> Sequence[CustomerNote]:
    rows = await session.scalars(
        select(CustomerNote)
        .where(CustomerNote.customer_id == customer_id)
        .order_by(CustomerNote.created_at, CustomerNote.note_id)
    )
    return rows.all()


def build_digest(notes: Sequence[CustomerNote]) -> tuple[str, datetime | None]:
    ordered = sorted(notes, key=lambda n: (n.created_at, n.note_id))
    digest = DIGEST_SEPARATOR.join(n.note_text for n in ordered)
    return digest, max((n.updated_at for n in ordered), default=None)


async def refresh_notes_digest(session: AsyncSession, customer: Customer) -> None:
    await session.flush()
    customer.customer_notes, customer.notes_updated_at = build_digest(
        await list_notes(session, customer.customer_id)
    )


async def _next_note_id(session: AsyncSession) -> str:
    # Inside the write transaction; SQLite allows one writer. On PostgreSQL the primary key
    # still rejects a concurrent duplicate, which surfaces as a retryable conflict.
    ids = await session.scalars(select(CustomerNote.note_id))
    numbers = [int(m.group(1)) for i in ids if (m := _NOTE_NUMBER.match(i))]
    return f"NOTE-{max(numbers, default=0) + 1:04d}"


async def add_note(
    session: AsyncSession,
    *,
    customer_id: str,
    note_type: NoteType,
    note_text: str,
    created_by: NoteCreatedBy,
    actor_type: ActorType,
    source_call_id: str | None = None,
    clock: Clock = utc_now,
) -> CustomerNote:
    """Create a note. The caller owns the transaction."""
    text = validate_note_text(note_text)
    customer = await get_customer_for_update(session, customer_id)
    now = clock()
    note = CustomerNote(
        note_id=await _next_note_id(session),
        customer_id=customer.customer_id,
        created_at=now,
        updated_at=now,
        note_type=note_type,
        note_text=text,
        created_by=created_by,
        source_call_id=source_call_id,
    )
    session.add(note)
    await refresh_notes_digest(session, customer)
    customer.updated_at = now
    record_audit(
        session,
        event_type="customer_note.created",
        entity_type="customer_note",
        entity_id=note.note_id,
        actor_type=actor_type,
        call_session_id=source_call_id,
        details={"customer_id": customer.customer_id, "note_type": note_type.value},
    )
    return note


async def update_note_text(
    session: AsyncSession,
    *,
    customer_id: str,
    note_id: str,
    note_text: str,
    actor_type: ActorType,
    call_session_id: str | None = None,
    clock: Clock = utc_now,
) -> CustomerNote:
    """Update a note owned by `customer_id`; the previous text is kept in the audit log."""
    text = validate_note_text(note_text)
    customer = await get_customer_for_update(session, customer_id)
    note = await session.get(CustomerNote, note_id)
    if note is None or note.customer_id != customer.customer_id:
        raise NotFoundError(f"Note {note_id} not found for customer {customer_id}")
    previous = note.note_text
    now = clock()
    note.note_text = text
    note.updated_at = now
    await refresh_notes_digest(session, customer)
    customer.updated_at = now
    record_audit(
        session,
        event_type="customer_note.updated",
        entity_type="customer_note",
        entity_id=note.note_id,
        actor_type=actor_type,
        call_session_id=call_session_id,
        details={"customer_id": customer.customer_id, "previous_text": previous},
    )
    return note


async def count_notes(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(CustomerNote)) or 0
