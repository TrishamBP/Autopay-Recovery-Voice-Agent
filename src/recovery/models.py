"""`recovery_case`: at most ONE active case per installment (partial unique index)."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base
from src.recovery.state_machine import TERMINAL, RecoveryCaseStatus, RecoveryOutcome

_ACTIVE_SQL = "status NOT IN ({})".format(", ".join(f"'{s.value}'" for s in sorted(TERMINAL)))


class RecoveryCase(Base):
    __tablename__ = "recovery_case"
    __table_args__ = (
        Index(
            "uq_recovery_case_active_installment",
            "installment_id",
            unique=True,
            sqlite_where=text(_ACTIVE_SQL),
            postgresql_where=text(_ACTIVE_SQL),
        ),
        Index("ix_recovery_case_simulation_status", "simulation_id", "status"),
    )

    recovery_case_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    simulation_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_state.simulation_id", ondelete="CASCADE")
    )
    installment_id: Mapped[str] = mapped_column(
        ForeignKey("billing_installment.installment_id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[RecoveryCaseStatus] = mapped_column(enum_column(RecoveryCaseStatus, "status"))
    opened_at_virtual: Mapped[datetime] = mapped_column(UTCDateTime)
    eligible_at: Mapped[datetime] = mapped_column(UTCDateTime)  # virtual
    approval_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # virtual
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # real (operator action)
    approved_at_virtual: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approval_reason: Mapped[str | None] = mapped_column(String(300))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # virtual
    cancellation_reason: Mapped[str | None] = mapped_column(String(100))
    recovery_outcome: Mapped[RecoveryOutcome | None] = mapped_column(
        enum_column(RecoveryOutcome, "recovery_outcome")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
