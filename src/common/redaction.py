"""Masking and sanitising untrusted or personal values before they reach logs or responses."""

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Same patterns the dataset generator uses to keep secrets out of seed data.
_SENSITIVE_WORDS = re.compile(
    r"\b(cvv|otp|upi pin|pin|password|card number|account number|ifsc)\b", re.IGNORECASE
)
_LONG_DIGITS = re.compile(r"\d{9,}")


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    return f"{phone[:3]}******{phone[-4:]}" if len(phone) > 7 else "******"


def sanitize_for_log(value: str, max_length: int = 200) -> str:
    """Strip control characters/newlines (log injection) and truncate."""
    cleaned = _CONTROL.sub(" ", value).strip()
    return cleaned if len(cleaned) <= max_length else cleaned[: max_length - 3] + "..."


def contains_secret_like_content(text: str) -> bool:
    """True for text that looks like it carries card numbers, OTPs, PINs or credentials."""
    return bool(_SENSITIVE_WORDS.search(text) or _LONG_DIGITS.search(text))


# --- Transcript redaction --------------------------------------------------------------------
# Speech-to-text writes numbers with spaces/dashes ("4111 1111 1111 1111"), so digit runs are
# matched across separators. Short numbers (amounts, dates, times) are kept.
_SECRET_AFTER_KEYWORD = re.compile(
    r"\b(otp|one[- ]time (?:password|code)|pin|upi pin|cvv|cvc|security code|password|passcode|"
    r"verification code)\b(\s*(?:is|was|:|=|number)?\s*)([A-Za-z0-9][A-Za-z0-9\s-]{0,20})",
    re.IGNORECASE,
)
_CARD_LIKE = re.compile(r"(?<!\d)(?:\d[\s-]?){11,18}\d(?!\d)")
_LONG_NUMBER = re.compile(r"(?<!\d)(?:\d[\s-]?){5,}\d(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"\+\d(?:[\s-]?\d){7,14}")


def strip_secrets(text: str) -> str:
    """Remove authentication secrets and card-like numbers. Applied to EVERY stored transcript
    field, including the optional original text."""
    text = _SECRET_AFTER_KEYWORD.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]", text)
    return _CARD_LIKE.sub("[REDACTED_CARD]", text)


def redact_transcript(text: str) -> str:
    """Sanitized display/audit text: secrets, card numbers, other long numbers, phone numbers
    and email addresses removed; control characters stripped."""
    text = _PHONE.sub("[REDACTED_PHONE]", _CONTROL.sub(" ", text))
    text = strip_secrets(text)
    text = _LONG_NUMBER.sub("[REDACTED_NUMBER]", text)
    return _EMAIL.sub("[REDACTED_EMAIL]", text).strip()
