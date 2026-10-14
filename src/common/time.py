"""Time helpers. Everything internal is aware UTC; IST is only an input/display concern."""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def ist_naive_to_utc(value: datetime) -> datetime:
    """Workbook timestamps are timezone-naive IST; convert to aware UTC."""
    if value.tzinfo is not None:
        raise ValueError("expected a naive IST datetime")
    return value.replace(tzinfo=IST).astimezone(UTC)
