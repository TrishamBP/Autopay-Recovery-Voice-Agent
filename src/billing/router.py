"""Installment endpoints. Read-only except the explicit, dev-only scenario injection; there is no
endpoint that sets an installment status directly."""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from src.billing.models import BillingInstallment, PaymentAttempt
from src.billing.state_machine import AttemptOutcome, InstallmentStatus
from src.common.dependencies import OperatorDep, SessionDep
from src.common.exceptions import NotFoundError
from src.common.money import paise_to_rupees
from src.common.schemas import ErrorResponse, Money, Page
from src.simulation import service as simulation_service
from src.simulation.models import SimulationState
from src.simulation.router import require_dev

router = APIRouter(
    prefix="/installments",
    tags=["installments"],
    dependencies=[OperatorDep],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)


class InstallmentOut(BaseModel):
    installment_id: str
    simulation_id: str
    subscription_id: str
    customer_id: str
    billing_period: str
    due_at: datetime
    amount: Money
    amount_minor_units: int
    currency: str
    payment_status: InstallmentStatus
    scenario_id: str
    recovery_eligible_at: datetime | None
    paid_at_virtual: datetime | None
    payment_source: str | None

    @classmethod
    def from_model(cls, i: BillingInstallment, tz: ZoneInfo) -> "InstallmentOut":
        return cls(
            installment_id=i.installment_id,
            simulation_id=i.simulation_id,
            subscription_id=i.subscription_id,
            customer_id=i.customer_id,
            billing_period=i.billing_period,
            due_at=i.due_at.astimezone(tz),
            amount=paise_to_rupees(i.amount_minor_units),
            amount_minor_units=i.amount_minor_units,
            currency=i.currency.value,
            payment_status=i.payment_status,
            scenario_id=i.scenario_id,
            recovery_eligible_at=i.recovery_eligible_at.astimezone(tz)
            if i.recovery_eligible_at
            else None,
            paid_at_virtual=i.paid_at_virtual.astimezone(tz) if i.paid_at_virtual else None,
            payment_source=i.payment_source.value if i.payment_source else None,
        )


class AttemptOut(BaseModel):
    payment_attempt_id: str
    attempt_number: int
    outcome: AttemptOutcome
    failure_reason: str | None
    attempted_at_virtual: datetime
    processed_at_real: datetime
    reconciled_at_virtual: datetime | None
    scenario_id: str


class InjectScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    reason: str = Field(min_length=3, max_length=200)


async def _tz(session: SessionDep, simulation_id: str) -> ZoneInfo:
    sim = await session.get(SimulationState, simulation_id)
    return ZoneInfo(sim.timezone) if sim else ZoneInfo("UTC")


@router.get("", response_model=Page[InstallmentOut])
async def list_installments(
    simulation_id: Annotated[str, Query(min_length=1, max_length=64)],
    session: SessionDep,
    customer_id: Annotated[str | None, Query(pattern=r"^CUS-\d{4}$")] = None,
    payment_status: InstallmentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[InstallmentOut]:
    conditions = [BillingInstallment.simulation_id == simulation_id]
    if customer_id:
        conditions.append(BillingInstallment.customer_id == customer_id)
    if payment_status:
        conditions.append(BillingInstallment.payment_status == payment_status)
    total = (
        await session.scalar(
            select(func.count()).select_from(BillingInstallment).where(*conditions)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(BillingInstallment)
            .where(*conditions)
            .order_by(BillingInstallment.due_at, BillingInstallment.subscription_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    tz = await _tz(session, simulation_id)
    return Page(
        items=[InstallmentOut.from_model(i, tz) for i in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _get(session: SessionDep, installment_id: str) -> BillingInstallment:
    installment = await session.get(BillingInstallment, installment_id)
    if installment is None:
        raise NotFoundError("Installment not found")
    return installment


@router.get("/{installment_id}", response_model=InstallmentOut)
async def get_installment(installment_id: str, session: SessionDep) -> InstallmentOut:
    installment = await _get(session, installment_id)
    return InstallmentOut.from_model(installment, await _tz(session, installment.simulation_id))


@router.get("/{installment_id}/attempts", response_model=list[AttemptOut])
async def list_attempts(installment_id: str, session: SessionDep) -> list[AttemptOut]:
    installment = await _get(session, installment_id)
    tz = await _tz(session, installment.simulation_id)
    attempts = (
        await session.scalars(
            select(PaymentAttempt)
            .where(PaymentAttempt.installment_id == installment_id)
            .order_by(PaymentAttempt.attempt_number)
        )
    ).all()
    return [
        AttemptOut(
            payment_attempt_id=a.payment_attempt_id,
            attempt_number=a.attempt_number,
            outcome=a.outcome,
            failure_reason=a.failure_reason.value if a.failure_reason else None,
            attempted_at_virtual=a.attempted_at_virtual.astimezone(tz),
            processed_at_real=a.processed_at_real,
            reconciled_at_virtual=a.reconciled_at_virtual.astimezone(tz)
            if a.reconciled_at_virtual
            else None,
            scenario_id=a.scenario_id,
        )
        for a in attempts
    ]


@router.post(
    "/{installment_id}/inject-scenario",
    response_model=InstallmentOut,
    dependencies=[Depends(require_dev)],
)
async def inject_scenario(
    installment_id: str, body: InjectScenario, request: Request, session: SessionDep
) -> InstallmentOut:
    installment = await simulation_service.inject_scenario(
        request.app.state.session_factory,
        request.app.state.processor,
        installment_id=installment_id,
        scenario_id=body.scenario_id,
        reason=body.reason,
    )
    return InstallmentOut.from_model(installment, await _tz(session, installment.simulation_id))
