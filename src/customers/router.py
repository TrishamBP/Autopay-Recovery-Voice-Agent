"""Operator read API for customers, payment history and notes (+ audited note writes)."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from src.common.dependencies import OperatorDep, SessionDep
from src.common.enums import ActorType, NoteCreatedBy
from src.common.schemas import ErrorResponse, Page
from src.customer_notes import service as notes_service
from src.customer_notes.schemas import NoteCreate, NoteItem, NoteUpdate
from src.customers import service
from src.customers.schemas import CustomerDetail, CustomerListItem
from src.payment_history import service as history_service
from src.payment_history.schemas import PaymentHistoryItem

router = APIRouter(
    prefix="/customers",
    tags=["customers"],
    dependencies=[OperatorDep],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
CustomerId = Annotated[str, Path(pattern=r"^CUS-\d{4}$")]


@router.get("", response_model=Page[CustomerListItem])
async def list_customers(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CustomerListItem]:
    rows, total = await service.list_customers(session, limit=limit, offset=offset)
    return Page(
        items=[CustomerListItem.from_model(c) for c in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(customer_id: CustomerId, session: SessionDep) -> CustomerDetail:
    return CustomerDetail.from_model(await service.get_customer(session, customer_id))


@router.get("/{customer_id}/payment-history", response_model=list[PaymentHistoryItem])
async def get_payment_history(
    customer_id: CustomerId, session: SessionDep
) -> list[PaymentHistoryItem]:
    await service.get_customer(session, customer_id)
    rows = await history_service.list_history(session, customer_id)
    return [PaymentHistoryItem.from_model(h) for h in rows]


@router.get("/{customer_id}/notes", response_model=list[NoteItem])
async def list_notes(customer_id: CustomerId, session: SessionDep) -> list[NoteItem]:
    await service.get_customer(session, customer_id)
    return [
        NoteItem.model_validate(n) for n in await notes_service.list_notes(session, customer_id)
    ]


@router.post(
    "/{customer_id}/notes",
    response_model=NoteItem,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"model": ErrorResponse}},
)
async def create_note(customer_id: CustomerId, body: NoteCreate, session: SessionDep) -> NoteItem:
    async with session.begin():
        note = await notes_service.add_note(
            session,
            customer_id=customer_id,
            note_type=body.note_type,
            note_text=body.note_text,
            created_by=NoteCreatedBy.HUMAN_AGENT,
            actor_type=ActorType.OPERATOR,
        )
    return NoteItem.model_validate(note)


@router.patch(
    "/{customer_id}/notes/{note_id}",
    response_model=NoteItem,
    responses={422: {"model": ErrorResponse}},
)
async def update_note(
    customer_id: CustomerId,
    note_id: Annotated[str, Path(pattern=r"^NOTE-\d{4,}$")],
    body: NoteUpdate,
    session: SessionDep,
) -> NoteItem:
    async with session.begin():
        note = await notes_service.update_note_text(
            session,
            customer_id=customer_id,
            note_id=note_id,
            note_text=body.note_text,
            actor_type=ActorType.OPERATOR,
        )
    return NoteItem.model_validate(note)
