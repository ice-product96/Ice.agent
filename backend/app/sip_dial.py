"""Outbound SIP dial target validation and normalization."""

from __future__ import annotations

import re
from typing import Any


class SipDialError(ValueError):
    """Invalid or unusable dial target before INVITE is sent."""


def normalize_sip_dial_number(value: str) -> str:
    """Normalize to digits-only E.164 body for Telphin (e.g. 79001234567)."""
    raw = str(value or "").strip()
    if not raw:
        raise SipDialError("Phone number is required")
    digits = re.sub(r"[^\d]", "", raw)
    if raw.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if not digits:
        raise SipDialError("Phone number must contain digits")
    return digits


def _telegram_identity_digits(context: dict[str, Any] | None) -> set[str]:
    ids: set[str] = set()
    if not context:
        return ids
    for key in ("sender_id", "chat_id", "user_id"):
        raw = str(context.get(key) or "").strip().lstrip("-")
        if raw.isdigit():
            ids.add(raw)
            if len(raw) == 10 and raw.startswith("7"):
                ids.add(f"7{raw}")
            if len(raw) == 11 and raw.startswith("7"):
                ids.add(raw[1:])
    return ids


def validate_sip_dial_target(number: str, context: dict[str, Any] | None = None) -> str:
    """
    Validate dial target and return normalized digits.
    Rejects Telegram user/chat ids commonly mistaken for phone numbers.
    """
    normalized = normalize_sip_dial_number(number)
    identities = _telegram_identity_digits(context)
    if normalized in identities:
        raise SipDialError(
            "This looks like a Telegram user/chat id, not a phone number. "
            "Ask the customer for their mobile number (+7…)."
        )
    if len(normalized) < 10 or len(normalized) > 15:
        raise SipDialError(
            f"Invalid phone length ({len(normalized)} digits). "
            "Use a full mobile number, e.g. +79001234567."
        )
    if len(normalized) == 10:
        raise SipDialError(
            "Phone number is too short (10 digits). "
            "Use the full number with country code, e.g. +79001234567."
        )
    if normalized.startswith("7") and len(normalized) == 11 and normalized[1] != "9":
        raise SipDialError(
            "This does not look like a valid RU mobile number (expected 79XXXXXXXXX). "
            "Confirm the number with the customer."
        )
    return normalized


def sip_failure_customer_message(exc: BaseException) -> str:
    """Short customer-facing message without SIP/tool internals."""
    text = str(exc).strip()
    lower = text.lower()
    if isinstance(exc, SipDialError) or "telegram user/chat id" in lower:
        return "Чтобы позвонить, пришлите, пожалуйста, ваш номер телефона (+7…)."
    if "403" in lower or "forbidden" in lower:
        return (
            "Не удалось дозвониться — оператор отклонил вызов. "
            "Проверьте номер или напишите его ещё раз."
        )
    if "empty number" in lower or "phone number is required" in lower:
        return "Чтобы позвонить, пришлите, пожалуйста, ваш номер телефона (+7…)."
    if "max concurrent" in lower:
        return "Сейчас линия занята другим звонком. Попробуйте через минуту."
    return "Сейчас не получилось дозвониться. Пришлите номер ещё раз или попробуйте позже."


def sip_failure_admin_message(exc: BaseException, *, number: str = "") -> str:
    text = str(exc).strip()
    lines = [f"SIP dial failed{f' → {number}' if number else ''}: {text}"]
    lower = text.lower()
    if "403" in lower:
        lines.append(
            "Telphin 403: проверьте права исходящей связи, формат номера (79XXXXXXXXX) "
            "и Public IP/RTP в настройках SIP-аккаунта."
        )
    if isinstance(exc, SipDialError):
        lines.append("Агент, вероятно, передал Telegram id вместо телефона.")
    return "\n".join(lines)
