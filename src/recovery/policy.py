"""Approval-time contact policy. Every rule here is enforced in code; none is a prompt.

Contact window: `customer.preferred_contact_time` is free text such as
"Weekdays 6 PM - 8 PM IST", "Any day 11 AM - 1 PM IST", "Weekends 10 AM - 1 PM IST" or
"Weekdays after 7 PM IST". It is parsed into days + hours and intersected with the global
calling hours (CALLING_HOURS_START/END). It is evaluated at the simulation's VIRTUAL time.
Unparseable text blocks the call rather than guessing.

Consent: `synthetic_test_record` is not real consent. It is accepted ONLY when
ALLOW_SYNTHETIC_CONSENT_FOR_TEST_DESTINATIONS=true AND the destination is an allowlisted
tester-controlled number (the tester, not the fictional customer, is the person called).
"""

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.common.enums import ContactConsent
from src.config import Settings
from src.customers.models import Customer

_WINDOW = re.compile(
    r"^(?P<days>weekdays|weekends|any day)\s+"
    r"(?:after\s+(?P<after>\d{1,2}(?::\d{2})?\s*[ap]m)"
    r"|(?P<start>\d{1,2}(?::\d{2})?\s*[ap]m)\s*-\s*(?P<end>\d{1,2}(?::\d{2})?\s*[ap]m))"
    r"\s*(?P<tz>ist)?$",
    re.IGNORECASE,
)
_DAYS = {
    "weekdays": frozenset(range(5)),
    "weekends": frozenset({5, 6}),
    "any day": frozenset(range(7)),
}


@dataclass(frozen=True)
class ContactWindow:
    days: frozenset[int]  # Monday = 0
    start: time
    end: time  # exclusive
    tz: ZoneInfo


@dataclass(frozen=True)
class Denial:
    code: str
    message: str


def _clock(text: str) -> time:
    text = text.strip().lower().replace(" ", "")
    meridiem, digits = text[-2:], text[:-2]
    hour, _, minute = digits.partition(":")
    h, m = int(hour), int(minute or 0)
    if not (1 <= h <= 12 and 0 <= m < 60):
        raise ValueError(f"invalid clock time {text!r}")
    return time((h % 12) + (12 if meridiem == "pm" else 0), m)


def _calling_end(settings: Settings) -> time:
    return (
        time(23, 59, 59) if settings.calling_hours_end >= 24 else time(settings.calling_hours_end)
    )


def parse_contact_window(
    text: str, default_tz: ZoneInfo, settings: Settings
) -> ContactWindow | None:
    match = _WINDOW.match(text.strip())
    if match is None:
        return None
    try:
        if match["after"]:
            start, end = _clock(match["after"]), _calling_end(settings)
        else:
            start, end = _clock(match["start"]), _clock(match["end"])
    except ValueError:
        return None
    if end <= start:
        return None
    tz = ZoneInfo("Asia/Kolkata") if match["tz"] else default_tz
    return ContactWindow(_DAYS[match["days"].lower()], start, end, tz)


def contact_window_denial(
    customer: Customer, at: datetime, default_tz: ZoneInfo, settings: Settings
) -> Denial | None:
    window = parse_contact_window(customer.preferred_contact_time, default_tz, settings)
    if window is None:
        return Denial(
            "contact_window_unknown",
            "The customer's preferred contact window could not be interpreted",
        )
    local = at.astimezone(window.tz)
    now = local.time()
    calling_start = time(settings.calling_hours_start)
    calling_end = _calling_end(settings)
    inside = (
        local.weekday() in window.days
        and window.start <= now < window.end
        and calling_start <= now < calling_end
    )
    if not inside:
        return Denial(
            "outside_contact_window",
            f"Virtual time {local.isoformat()} is outside the customer's window "
            f"'{customer.preferred_contact_time}' or calling hours "
            f"{settings.calling_hours_start:02d}:00-{settings.calling_hours_end:02d}:00",
        )
    return None


def consent_denial(customer: Customer, settings: Settings) -> Denial | None:
    if (
        customer.customer_contact_consent == ContactConsent.SYNTHETIC_TEST_RECORD
        and settings.allow_synthetic_consent_for_test_destinations
    ):
        return None
    return Denial(
        "consent_invalid",
        "No valid contact consent for this call (synthetic records are not consent)",
    )


def destination_denial(destination: str, settings: Settings) -> Denial | None:
    if destination not in settings.allowed_test_destinations:
        return Denial(
            "destination_not_allowlisted", "Destination is not in TEST_DESTINATION_ALLOWLIST"
        )
    return None
