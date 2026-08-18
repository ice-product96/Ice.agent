"""Turn scheduled-job outcomes into a short Russian summary for the UI."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .action_reports import audit_tool_result
from .integrations import exception_text

logger = logging.getLogger(__name__)

SKIP_REASONS = {
    "agent_missing": "Агент не найден.",
    "agent_disabled": "Агент выключен — запуск пропущен.",
    "autonomy_disabled": "Автономия выключена — тик пропущен.",
    "paused": "Сотрудник на паузе — тик пропущен.",
    "budget_exhausted": "Исчерпан дневной лимит рабочих тиков.",
    "off_hours": "Вне рабочих часов — тик пропущен.",
    "ok": "Рабочий тик выполнен.",
    "no_open_work": "Открытых кейсов нет — сторож завершился без эскалации.",
}

PUBLIC_RESULT_KEYS = ("ok", "status", "title", "summary", "details", "ran_at")
ORIGIN_CHAT_KEYS = ("reply_chat_id", "chat_id", "entity", "sender_id")


def origin_chat_id(payload: dict[str, Any] | None) -> Any:
    source = payload or {}
    for key in ORIGIN_CHAT_KEYS:
        value = source.get(key)
        if value not in (None, "", False):
            return value
    return None


def origin_phone(payload: dict[str, Any] | None, account_phone: str | None = None) -> str | None:
    source = payload or {}
    for value in (source.get("reply_phone"), source.get("phone"), account_phone):
        text = str(value or "").strip()
        if text:
            return text
    return None


def build_followup_payload(
    *,
    message: str,
    run_at_iso: str,
    timezone: str,
    context: dict[str, Any] | None,
    account_phone: str | None,
) -> dict[str, Any]:
    source = context or {}
    chat_id = origin_chat_id(source)
    phone = origin_phone(source, account_phone)
    return {
        "message": message,
        "run_once_at": run_at_iso,
        "timezone": timezone,
        "source": "scheduled",
        "reply_to_chat": bool(chat_id),
        "reply_phone": phone,
        "reply_chat_id": chat_id,
        "chat_id": chat_id,
        "phone": phone,
        "sender_id": source.get("sender_id") or chat_id,
        "sender_username": source.get("sender_username"),
        "is_admin": bool(source.get("is_admin")),
        "message_id": source.get("message_id"),
        "work_item_id": source.get("work_item_id"),
    }


def collect_origin_from_jobs(jobs: list[Any]) -> dict[str, Any]:
    for job in jobs:
        payload = getattr(job, "payload", None) or {}
        chat_id = origin_chat_id(payload)
        if chat_id in (None, "", False):
            continue
        phone = origin_phone(payload)
        return {
            "reply_chat_id": chat_id,
            "chat_id": payload.get("chat_id") or chat_id,
            "reply_phone": phone,
            "phone": phone,
            "sender_id": payload.get("sender_id") or chat_id,
            "sender_username": payload.get("sender_username"),
            "is_admin": payload.get("is_admin"),
            "message_id": payload.get("message_id"),
        }
    return {}


def telegram_already_sent(audit: list[dict[str, Any]] | None) -> bool:
    for call in audit or []:
        if not isinstance(call, dict):
            continue
        if str(call.get("tool") or "") == "telegram_send_message" and call.get("status") == "success":
            return True
    return False


def coerce_telegram_entity(chat_id: Any) -> Any:
    raw = str(chat_id).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


async def send_origin_reply(
    telegram: Any,
    phone: str | None,
    chat_id: Any,
    text: str,
) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {"sent": False, "reason": "нет текста результата."}
    if telegram is None:
        return {"sent": False, "reason": "Telegram не подключён."}
    if not phone:
        return {"sent": False, "reason": "нет Telegram-аккаунта агента."}
    if chat_id in (None, "", False):
        return {"sent": False, "reason": "не сохранён исходный чат заказчика."}
    try:
        entity = coerce_telegram_entity(chat_id)
        await telegram.send_message(str(phone), entity, cleaned)
        return {"sent": True, "chat_id": entity, "phone": str(phone)}
    except Exception as exc:
        logger.exception("Failed to deliver scheduled result to origin Telegram chat")
        return {"sent": False, "reason": exception_text(exc)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, limit: int = 700) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def describe_value(value: Any) -> str:
    value = _maybe_json(value)
    if value is None or value == "":
        return ""
    if isinstance(value, dict):
        if "done" in value:
            summary = str(value.get("summary") or "").strip()
            if value.get("done"):
                return _clip("Cursor закончил работу." + (f" {summary}" if summary else ""))
            status = str(value.get("status") or "").strip()
            extra = f" ({status})" if status else ""
            return _clip(f"Cursor ещё работает{extra}.")
        if value.get("skipped"):
            reason = str(value.get("reason") or "skipped")
            return SKIP_REASONS.get(reason, _clip(str(value.get("reason") or "Пропущено.")))
        for key in ("customer_reply", "detail", "summary", "message", "text", "result"):
            inner = value.get(key)
            if inner not in (None, "", [], {}):
                described = describe_value(inner)
                if described:
                    return described
        if value.get("ok") is False:
            return _clip(str(value.get("error") or value.get("reason") or "Задача завершилась с ошибкой."))
        return ""
    if isinstance(value, list):
        parts = [describe_value(item) for item in value[:5]]
        return _clip(" ".join(part for part in parts if part))
    text = str(value).strip()
    if text[:1] in "{[":
        return describe_value(_maybe_json(text))
    return _clip(text)


def notes_from_audit(audit: list[dict[str, Any]] | None) -> list[str]:
    notes: list[str] = []
    for call in audit or []:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        payload = audit_tool_result(call)
        if tool in {"cursorremote_check", "cursorremote_do"} and isinstance(payload, dict):
            if payload.get("done"):
                summary = str(payload.get("summary") or "").strip()
                notes.append("Cursor закончил работу" + (f": {summary[:240]}" if summary else "."))
            else:
                status = str(payload.get("status") or "").strip()
                notes.append(
                    "Cursor ещё не закончил"
                    + (f" ({status})" if status else "")
                    + " — нужна повторная проверка."
                )
        elif tool == "schedule_self":
            if isinstance(payload, dict) and payload.get("skipped"):
                notes.append("Повторная проверка не ставилась: задача уже завершена.")
            elif call.get("status") == "success":
                notes.append("Поставлена повторная проверка позже.")
        elif tool == "telegram_send_message" and call.get("status") == "success":
            notes.append("Сообщение отправлено в Telegram.")
        elif tool == "sip_dial":
            if isinstance(payload, dict) and payload.get("ok"):
                notes.append("Исходящий звонок выполнен.")
            elif call.get("status") == "success":
                notes.append("Попытка исходящего звонка завершена.")
    seen: list[str] = []
    for note in notes:
        if note not in seen:
            seen.append(note)
    return seen[:8]


def public_job_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in PUBLIC_RESULT_KEYS if value.get(key) not in (None, "")}
    if not result.get("summary") and not result.get("title"):
        return None
    details = result.get("details")
    if isinstance(details, list):
        result["details"] = [str(item) for item in details if str(item).strip()][:8]
    return result


def _friendly_job_error(error: BaseException) -> str:
    text = exception_text(error)
    lower = text.lower()
    if "pendingrollback" in lower:
        return "Сбой записи в базу во время запуска. Повторная задача будет с другим именем."
    if "uniqueviolation" in lower or "duplicate key" in lower or "cron_jobs_name_key" in lower:
        return "Имя задачи в расписании уже занято. Повтор будет поставлен под новым именем."
    return _clip(text, 400)


def humanize_job_outcome(
    raw: Any,
    *,
    payload: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    ran_at = _utc_now()
    if error is not None:
        return {
            "ok": False,
            "status": "error",
            "title": "Ошибка",
            "summary": _friendly_job_error(error),
            "details": [],
            "ran_at": ran_at,
        }

    details = list(payload.get("_job_notes") or [])

    if isinstance(raw, dict) and raw.get("skipped"):
        reason = str(raw.get("reason") or "skipped")
        return {
            "ok": True,
            "status": "skipped",
            "title": "Пропущено",
            "summary": SKIP_REASONS.get(reason, _clip(f"Пропущено: {reason}")),
            "details": details[:8],
            "ran_at": ran_at,
        }

    if str(payload.get("source") or "") == "employee_heartbeat" and not (
        isinstance(raw, dict) and raw.get("deliver_origin")
    ):
        watchdog = raw.get("watchdog") if isinstance(raw, dict) else None
        count = watchdog.get("count") if isinstance(watchdog, dict) else None
        summary = (
            f"Проверено открытых кейсов: {count}."
            if count is not None
            else "Сторожевой тик без эскалации руководителю."
        )
        return {
            "ok": True,
            "status": "completed",
            "title": "Сторож",
            "summary": summary,
            "details": [],
            "ran_at": ran_at,
        }

    text = ""
    if isinstance(raw, dict):
        extra_notes = raw.get("notes")
        if isinstance(extra_notes, list):
            details.extend(str(item) for item in extra_notes if str(item).strip())
        delivery = raw.get("delivery")
        if isinstance(delivery, dict):
            if delivery.get("sent"):
                details.append("Результат отправлен в исходный чат Telegram.")
            else:
                reason = str(delivery.get("reason") or "").strip()
                details.append("Заказчику не отправлено" + (f": {reason}" if reason else "."))
        elif raw.get("notified"):
            details.append("Результат отправлен в исходный чат Telegram.")
        text = describe_value(raw.get("result") if "result" in raw else raw)
    elif raw not in (None, ""):
        text = describe_value(raw)

    unique_details: list[str] = []
    for item in details:
        note = _clip(str(item), 240)
        if note and note not in unique_details and note != text:
            unique_details.append(note)

    if not text:
        text = unique_details[0] if unique_details else "Задача отработала, текстового ответа агент не оставил."

    return {
        "ok": True,
        "status": "completed",
        "title": "Выполнено",
        "summary": _clip(text, 700),
        "details": unique_details[:8],
        "ran_at": ran_at,
    }
