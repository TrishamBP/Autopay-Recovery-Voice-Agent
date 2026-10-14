"""`payment_link`: SIMULATED payment / payment-method-update links.

A link is an opaque random token pointing at a non-resolvable `.invalid` URL. It never collects
payment details, never charges anything and is never read aloud or returned to the LLM.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.enums import PaymentLinkPurpose, PaymentLinkStatus
from src.common.ids import new_id
from src.common.time import utc_now
from src.common.types import UTCDateTime, enum_column
from src.database import Base


class PaymentLink(Base):
    __tablename__ = "payment_link"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), index=True
    )
    call_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("call_session.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[PaymentLinkPurpose] = mapped_column(enum_column(PaymentLinkPurpose, "purpose"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[PaymentLinkStatus] = mapped_column(
        enum_column(PaymentLinkStatus, "status"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
