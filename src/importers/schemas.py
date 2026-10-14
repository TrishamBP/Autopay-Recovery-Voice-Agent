"""Stages 3-4: typed, validated import rows. One model per data sheet.

Cell coercion rules (strict; anything else is a validation error, never a guess):
- dates:       `date`, a midnight `datetime` (Excel's date cells), or ISO `YYYY-MM-DD` text
- timestamps:  naive `datetime` or ISO text, interpreted as IST and converted to aware UTC
- money:       int / Decimal / float / text like "799", "1,499.00", "₹799.00" -> 2dp Decimal
- integers:    int, integral float, or digit text (bools rejected)
- booleans:    bool or "true"/"false" text (case-insensitive)
- text:        stripped; empty -> None for optional fields
"""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from src.common.enums import (
    FULLY_PAID_STATUSES,
    UNPAID_STATUSES,
    AgentToolAction,
    BillingFrequency,
    CallOutcome,
    CallStatus,
    ContactConsent,
    Currency,
    CustomerIntent,
    NoteCreatedBy,
    NoteType,
    PaymentFailureReason,
    PaymentHistoryStatus,
    PaymentLinkStatus,
    PaymentMethodType,
    PaymentStatus,
    RecoveryStatus,
    SupportedLanguage,
)
from src.common.money import to_decimal
from src.common.redaction import contains_secret_like_content
from src.common.time import ist_naive_to_utc
from src.customer_notes.models import NOTE_MAX_LENGTH

E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def _blank_to_none(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _parse_date(value: object) -> object:
    value = _blank_to_none(value)
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return value
    if isinstance(value, datetime):
        if value.time() != datetime.min.time():
            raise ValueError("expected a date without a time component")
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"expected a date, got {type(value).__name__}")


def _parse_timestamp(value: object) -> object:
    value = _blank_to_none(value)
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return ist_naive_to_utc(value) if value.tzinfo is None else value
    raise ValueError(f"expected a timestamp, got {type(value).__name__}")


def _parse_money(value: object) -> object:
    value = _blank_to_none(value)
    return None if value is None else to_decimal(value)


def _parse_int(value: object) -> object:
    value = _blank_to_none(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected an integer, got a boolean")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("expected a whole number")
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    if isinstance(value, int):
        return value
    raise ValueError(f"expected an integer, got {value!r}")


def _parse_bool(value: object) -> object:
    value = _blank_to_none(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError(f"expected TRUE/FALSE, got {value!r}")


def _normalize_phone(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("phone_number must be text")
    digits = re.sub(r"[\s\-().]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif re.fullmatch(r"[6-9]\d{9}", digits):
        digits = "+91" + digits  # bare Indian mobile number
    if not E164.match(digits):
        raise ValueError("phone_number is not a valid E.164 number")
    return digits


Text = Annotated[str, BeforeValidator(_blank_to_none)]
OptText = Annotated[str | None, BeforeValidator(_blank_to_none)]
WbDate = Annotated[date, BeforeValidator(_parse_date)]
OptWbDate = Annotated[date | None, BeforeValidator(_parse_date)]
WbTimestamp = Annotated[datetime, BeforeValidator(_parse_timestamp)]
OptWbTimestamp = Annotated[datetime | None, BeforeValidator(_parse_timestamp)]
WbMoney = Annotated[Decimal, BeforeValidator(_parse_money)]
OptWbMoney = Annotated[Decimal | None, BeforeValidator(_parse_money)]
WbInt = Annotated[int, BeforeValidator(_parse_int)]
OptWbInt = Annotated[int | None, BeforeValidator(_parse_int)]
WbBool = Annotated[bool, BeforeValidator(_parse_bool)]
Phone = Annotated[str, BeforeValidator(_normalize_phone)]


class ImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerImport(ImportRow):
    customer_id: Text = Field(pattern=r"^CUS-\d{4}$")
    full_name: Text = Field(min_length=1, max_length=100)
    phone_number: Phone
    email: Text = Field(pattern=r"^[^@\s]+@example\.com$", max_length=254)
    subscription_id: Text = Field(pattern=r"^SUB-\d{4}$")
    subscription_plan: Text = Field(min_length=1, max_length=100)
    payment_amount: WbMoney = Field(gt=0)
    currency: Currency
    billing_frequency: BillingFrequency
    payment_due_date: WbDate
    payment_failure_date: OptWbDate
    payment_failure_reason: PaymentFailureReason | None
    payment_status: PaymentStatus
    payment_attempt_count: WbInt = Field(ge=1, le=3)
    last_payment_attempt_at: OptWbTimestamp
    next_retry_eligible_at: OptWbTimestamp
    payment_link_status: PaymentLinkStatus
    payment_link_url: OptText = Field(default=None, pattern=r"^https://", max_length=500)
    customer_contact_consent: ContactConsent
    preferred_language: SupportedLanguage
    preferred_contact_time: Text = Field(min_length=1, max_length=100)
    recovery_status: RecoveryStatus
    callback_requested_at: OptWbTimestamp
    do_not_contact: WbBool
    last_call_id: OptText = Field(default=None, max_length=64)
    last_call_status: CallStatus
    last_call_outcome: CallOutcome
    updated_at: WbTimestamp
    # Precomputed workbook summaries: compared against recomputed values, never trusted.
    payment_history_summary: Text
    payments_on_time_6m: WbInt = Field(ge=0)
    payments_late_6m: WbInt = Field(ge=0)
    payments_failed_6m: WbInt = Field(ge=0)
    payments_missed_6m: WbInt = Field(ge=0)
    minimum_amount_paid_6m: OptWbMoney = Field(default=None, gt=0)
    total_amount_paid_6m: WbMoney = Field(ge=0)
    payment_reliability_score: OptWbInt = Field(default=None, ge=0, le=100)
    customer_notes: OptText = None
    notes_updated_at: OptWbTimestamp = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (
            self.payment_status == PaymentStatus.PAID
            or self.recovery_status == RecoveryStatus.RECOVERED
        ):
            raise ValueError(
                "seed data may not mark a charge paid/recovered without a confirmation event"
            )
        if self.payment_status == PaymentStatus.FAILED and self.payment_failure_reason is None:
            raise ValueError("a failed charge needs payment_failure_reason")
        if (
            self.last_payment_attempt_at
            and self.payment_failure_date
            and self.last_payment_attempt_at.date() < self.payment_failure_date
        ):
            raise ValueError("last_payment_attempt_at is before payment_failure_date")
        return self


class PaymentHistoryImport(ImportRow):
    history_id: Text = Field(pattern=r"^HIST-\d{4}$")
    customer_id: Text = Field(pattern=r"^CUS-\d{4}$")
    billing_month: Text = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    due_date: WbDate
    amount_due: WbMoney = Field(gt=0)
    amount_paid: WbMoney = Field(ge=0)
    payment_status: PaymentHistoryStatus
    payment_date: OptWbDate
    days_late: OptWbInt = Field(default=None, ge=0)
    minimum_amount_paid: WbBool
    payment_method_type: PaymentMethodType
    failure_reason: PaymentFailureReason | None
    retry_count: WbInt = Field(ge=0, le=2)
    recovered_after_failure: WbBool
    notes: OptText = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        s, problems = self.payment_status, []
        if self.due_date.strftime("%Y-%m") != self.billing_month:
            problems.append("due_date falls outside billing_month")
        if self.amount_paid > self.amount_due:
            problems.append("amount_paid exceeds amount_due")
        if s in FULLY_PAID_STATUSES and self.amount_paid != self.amount_due:
            problems.append(f"{s} requires amount_paid == amount_due")
        if s == PaymentHistoryStatus.PARTIALLY_PAID and not 0 < self.amount_paid < self.amount_due:
            problems.append("partially_paid requires 0 < amount_paid < amount_due")
        if s in UNPAID_STATUSES and (
            self.amount_paid != 0 or self.payment_date is not None or self.days_late is not None
        ):
            problems.append(f"{s} cycle must have amount_paid 0, no payment_date and no days_late")
        if s not in UNPAID_STATUSES:
            if self.payment_date is None or self.days_late is None:
                problems.append(f"{s} requires payment_date and days_late")
            elif (self.payment_date - self.due_date).days != self.days_late:
                problems.append("days_late does not match payment_date - due_date")
        if s == PaymentHistoryStatus.PAID_ON_TIME and self.days_late not in (0, None):
            problems.append("paid_on_time requires days_late 0")
        if s == PaymentHistoryStatus.PAID_LATE and (self.days_late or 0) < 1:
            problems.append("paid_late requires days_late >= 1")
        if self.minimum_amount_paid != (s == PaymentHistoryStatus.PARTIALLY_PAID):
            problems.append("minimum_amount_paid must be true exactly for partially_paid")
        if self.recovered_after_failure and (
            s not in FULLY_PAID_STATUSES or not self.failure_reason
        ):
            problems.append("recovered_after_failure requires a failure followed by full payment")
        if self.failure_reason and s in FULLY_PAID_STATUSES and not self.recovered_after_failure:
            problems.append("a failure on a fully paid cycle must set recovered_after_failure")
        if s in (*UNPAID_STATUSES, PaymentHistoryStatus.PARTIALLY_PAID) and not self.failure_reason:
            problems.append(f"{s} requires failure_reason")
        if not self.failure_reason and self.retry_count != 0:
            problems.append("retry_count > 0 without a failure_reason")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class CustomerNoteImport(ImportRow):
    note_id: Text = Field(pattern=r"^NOTE-\d{4}$")
    customer_id: Text = Field(pattern=r"^CUS-\d{4}$")
    created_at: WbTimestamp
    note_type: NoteType
    note_text: Text = Field(min_length=1, max_length=NOTE_MAX_LENGTH)
    created_by: NoteCreatedBy
    source_call_id: OptText = None
    updated_at: WbTimestamp

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at is before created_at")
        if contains_secret_like_content(self.note_text):
            raise ValueError("note_text looks like it contains credentials or long digit sequences")
        if self.source_call_id is not None:
            raise ValueError(
                "seed notes cannot reference a call session (source_call_id must be blank)"
            )
        return self


class CallScenarioImport(ImportRow):
    customer_id: Text = Field(pattern=r"^CUS-\d{4}$")
    scenario_name: Text = Field(min_length=1, max_length=200)
    customer_persona: Text
    likely_opening_response: Text
    likely_customer_intent: CustomerIntent
    expected_agent_behavior: Text
    expected_tool_action: AgentToolAction
    expected_final_outcome: Text
    evaluation_notes: Text
