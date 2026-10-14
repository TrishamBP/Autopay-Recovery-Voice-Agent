"""Billing-date rules, evaluated in the simulation's time zone.

- monthly: due on the anchor day; months without that day use their last day
  (31st -> Apr 30, Feb 28; Feb 29 in leap years).
- annual: due on the anchor month/day; Feb 29 anchors fall on Feb 28 in non-leap years.
- A due date's instant is local midnight; the autopay attempt runs at the configured local time.
Weekly billing is not supported: the dataset's `billing_frequency` enum has only monthly/annual.
"""

import calendar
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from src.common.enums import BillingFrequency


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def local_instant(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def due_dates(
    frequency: BillingFrequency, anchor: date, start: date, end: date
) -> Iterator[tuple[str, date]]:
    """(billing_period, due_date) for every due date in [start, end], in order."""
    if frequency == BillingFrequency.MONTHLY:
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            due = _clamped(year, month, anchor.day)
            if start <= due <= end:
                yield f"{year:04d}-{month:02d}", due
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    elif frequency == BillingFrequency.ANNUAL:
        for year in range(start.year, end.year + 1):
            due = _clamped(year, anchor.month, anchor.day)
            if start <= due <= end:
                yield f"{year:04d}", due
    else:  # pragma: no cover - enum is exhaustive
        raise ValueError(f"unsupported billing frequency {frequency}")
