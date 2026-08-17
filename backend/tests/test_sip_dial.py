import pytest

from app.sip_dial import (
    SipDialError,
    normalize_sip_dial_number,
    sip_failure_customer_message,
    validate_sip_dial_target,
)


def test_normalize_ru_mobile() -> None:
    assert normalize_sip_dial_number("+7 (900) 123-45-67") == "79001234567"
    assert normalize_sip_dial_number("89001234567") == "79001234567"


def test_reject_telegram_sender_id_as_phone() -> None:
    ctx = {"sender_id": "7868511513", "chat_id": "7868511513"}
    with pytest.raises(SipDialError, match="Telegram user/chat id"):
        validate_sip_dial_target("7868511513", ctx)


def test_reject_invalid_ru_prefix() -> None:
    with pytest.raises(SipDialError, match="valid RU mobile"):
        validate_sip_dial_target("78685115123")


def test_customer_message_hides_sip_internals() -> None:
    text = sip_failure_customer_message(RuntimeError("Outbound call failed (SIP 403 Forbidden)"))
    assert "403" not in text
    assert "SIP" not in text
