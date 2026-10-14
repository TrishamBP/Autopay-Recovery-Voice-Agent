"""Recovery queue and manual approval. Approval is the ONLY way a call can be initiated."""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.calls.models import CallSession
from src.common.dependencies import OperatorDep, SessionDep, SettingsDep
from src.common.exceptions import NotFoundError, ValidationFailedError
from src.common.schemas import ErrorResponse, Page
from src.recovery import approval, service
from src.recovery.models import RecoveryCase
from src.recovery.state_machine import RecoveryCaseStatus
from src.simulation.models import SimulationState

router = APIRouter(
    prefix="/recovery",
    tags=["recovery"],
    dependencies=[OperatorDep],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)


class RecoveryCaseOut(BaseModel):
    recovery_case_id: str
    simulation_id: str
    installment_id: str
    customer_id: str
    status: RecoveryCaseStatus
    opened_at_virtual: datetime
    eligible_at: datetime
    approval_requested_at: datetime | None
    approved_at: datetime | None
    approved_at_virtual: datetime | None
    approved_by: str | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    recovery_outcome: str | None
    calls: list[dict[str, object]] = []

    @classmethod
    def from_model(
        cls, c: RecoveryCase, tz: ZoneInfo, calls: list[CallSession] | None = None
    ) -> "RecoveryCaseOut":
        def local(value: datetime | None) -> datetime | None:
            return value.astimezone(tz) if value else None

        return cls(
            recovery_case_id=c.recovery_case_id,
            simulation_id=c.simulation_id,
            installment_id=c.installment_id,
            customer_id=c.customer_id,
            status=c.status,
            opened_at_virtual=c.opened_at_virtual.astimezone(tz),
            eligible_at=c.eligible_at.astimezone(tz),
            approval_requested_at=local(c.approval_requested_at),
            approved_at=c.approved_at,
            approved_at_virtual=local(c.approved_at_virtual),
            approved_by=c.approved_by,
            cancelled_at=local(c.cancelled_at),
            cancellation_reason=c.cancellation_reason,
            recovery_outcome=c.recovery_outcome.value if c.recovery_outcome else None,
            calls=[
                {
                    "call_session_id": s.id,
                    "status": s.status.value,
                    "provider": s.provider,
                    "provider_call_id": s.provider_call_id,
                    "destination": s.destination_masked,
                }
                for s in (calls or [])
            ],
        )


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool = Field(description="Must be true; use /reject to decline")
    approved_by: str = Field(min_length=2, max_length=100)
    approval_reason: str = Field(min_length=3, max_length=300)
    test_destination: str = Field(
        pattern=r"^\+[1-9]\d{7,14}$", description="Must be in TEST_DESTINATION_ALLOWLIST"
    )


class ApproveResponse(BaseModel):
    recovery_case_id: str
    status: RecoveryCaseStatus
    call_session_id: str
    provider: str
    provider_call_id: str | None
    call_initiation: str
    replayed: bool


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rejected_by: str = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=3, max_length=300)


async def _tz(session: SessionDep, simulation_id: str) -> ZoneInfo:
    sim = await session.get(SimulationState, simulation_id)
    return ZoneInfo(sim.timezone) if sim else ZoneInfo("UTC")


@router.get("/queue", response_model=Page[RecoveryCaseOut])
async def approval_queue(
    simulation_id: Annotated[str, Query(min_length=1, max_length=64)],
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecoveryCaseOut]:
    rows, total = await service.list_cases(
        session,
        simulation_id=simulation_id,
        statuses=[RecoveryCaseStatus.AWAITING_APPROVAL],
        limit=limit,
        offset=offset,
    )
    tz = await _tz(session, simulation_id)
    return Page(
        items=[RecoveryCaseOut.from_model(c, tz) for c in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/cases", response_model=Page[RecoveryCaseOut])
async def list_cases(
    simulation_id: Annotated[str, Query(min_length=1, max_length=64)],
    session: SessionDep,
    status: RecoveryCaseStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecoveryCaseOut]:
    rows, total = await service.list_cases(
        session,
        simulation_id=simulation_id,
        statuses=[status] if status else None,
        limit=limit,
        offset=offset,
    )
    tz = await _tz(session, simulation_id)
    return Page(
        items=[RecoveryCaseOut.from_model(c, tz) for c in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{case_id}", response_model=RecoveryCaseOut)
async def get_case(case_id: str, session: SessionDep) -> RecoveryCaseOut:
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        raise NotFoundError("Recovery case not found")
    calls = (
        await session.scalars(
            select(CallSession)
            .where(CallSession.recovery_case_id == case_id)
            .order_by(CallSession.created_at)
        )
    ).all()
    return RecoveryCaseOut.from_model(case, await _tz(session, case.simulation_id), list(calls))


@router.post(
    "/{case_id}/approve", response_model=ApproveResponse, responses={502: {"model": ErrorResponse}}
)
async def approve(
    case_id: str,
    body: ApproveRequest,
    request: Request,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(min_length=8, max_length=128)] = None,
) -> ApproveResponse:
    if not body.approved:
        raise ValidationFailedError("approved must be true; use the reject endpoint instead")
    result = await approval.approve_case(
        request.app.state.session_factory,
        request.app.state.telephony,
        settings,
        case_id,
        approval.ApprovalRequest(
            body.approved_by, body.approval_reason, body.test_destination, idempotency_key
        ),
    )
    return ApproveResponse(**result.__dict__)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_destination: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


@router.post("/{case_id}/reconcile-call", response_model=ApproveResponse)
async def reconcile_call(
    case_id: str, body: ReconcileRequest, request: Request, settings: SettingsDep
) -> ApproveResponse:
    """Resolve an ambiguous call initiation (provider timeout) before any new approval."""
    result = await approval.reconcile_call(
        request.app.state.session_factory,
        request.app.state.telephony,
        settings,
        case_id,
        body.test_destination,
    )
    return ApproveResponse(**result.__dict__)


@router.post("/{case_id}/reject", response_model=RecoveryCaseOut)
async def reject(
    case_id: str, body: RejectRequest, request: Request, session: SessionDep
) -> RecoveryCaseOut:
    case = await approval.reject_case(
        request.app.state.session_factory, case_id, rejected_by=body.rejected_by, reason=body.reason
    )
    return RecoveryCaseOut.from_model(case, await _tz(session, case.simulation_id))
