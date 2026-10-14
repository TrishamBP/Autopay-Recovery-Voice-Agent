"""Exact money handling.

Storage: integer minor units (paise, 1 INR = 100 paise) in `*_paise` columns.
Application code: `Decimal` rupees with exactly two decimal places. Never `float`.
"""

import re
from decimal import Decimal, InvalidOperation

PAISE_PER_RUPEE = 100
TWO_PLACES = Decimal("0.01")
_AMOUNT_TEXT = re.compile(
    r"^\s*(?:₹|INR|Rs\.?)?\s*(-?[0-9][0-9,]*(?:\.[0-9]+)?)\s*$", re.IGNORECASE
)


class MoneyError(ValueError):
    pass


def to_decimal(value: object) -> Decimal:
    """Parse a workbook/API amount into an exact two-place Decimal.

    Accepts int, Decimal, float (via its shortest repr, so 499.9 stays 499.9) and text such as
    "799", "1,499.00", "₹799.00" or "INR 799". Rejects bools, NaN/inf and more than two decimals.
    """
    if isinstance(value, bool) or value is None:
        raise MoneyError(f"not an amount: {value!r}")
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, int):
            amount = Decimal(value)
        elif isinstance(value, float):
            amount = Decimal(repr(value))
        elif isinstance(value, str):
            match = _AMOUNT_TEXT.match(value)
            if not match:
                raise MoneyError(f"not an amount: {value!r}")
            amount = Decimal(match.group(1).replace(",", ""))
        else:
            raise MoneyError(f"not an amount: {value!r}")
    except InvalidOperation as exc:
        raise MoneyError(f"not an amount: {value!r}") from exc
    if not amount.is_finite():
        raise MoneyError(f"not a finite amount: {value!r}")
    if amount != amount.quantize(TWO_PLACES):
        raise MoneyError(f"more than two decimal places: {value!r}")
    return amount.quantize(TWO_PLACES)


def rupees_to_paise(amount: Decimal) -> int:
    paise = amount.quantize(TWO_PLACES) * PAISE_PER_RUPEE
    return int(paise)


def paise_to_rupees(paise: int) -> Decimal:
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(TWO_PLACES)


def optional_paise_to_rupees(paise: int | None) -> Decimal | None:
    return None if paise is None else paise_to_rupees(paise)


def format_inr(amount: Decimal) -> str:
    return f"INR {amount:,.2f}"
