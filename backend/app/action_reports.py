"""Split interlocutor replies from admin-facing action reports."""

from __future__ import annotations

import json
from typing import Any


READ_ONLY_TOOLS = {
    "web_search",
    "memory_search",
    "memory_add",
    "sleep",
    "parse_json",
    "telegram_suppress_reply",
    "telegram_get_dialogs",
    "telegram_get_history",
    "telegram_get_conversation_history",
    "telegram_get_messages",
    "telegram_get_participants",
    "telegram_get_drafts",
    "telegram_get_entity",
    "telegram_download_media",
    "telegram_acknowledge_read",
    "cursorremote_check",
    "schedule_self_list",
}

INTERNAL_FOLLOWUP_TOOLS = {
    "cursorremote_check",
    "schedule_self",
    "schedule_self_list",
    "schedule_self_cancel",
}

INTERNAL_EXECUTION_SOURCES = frozenset(
    {"intake_flush", "scheduled", "employee_tick", "employee_heartbeat"}
)


def is_side_effect_tool(name: str) -> bool:
    """True for mutating actions that admins should be notified about."""
    tool = str(name or "").strip()
    if not tool:
        return False
    if tool in READ_ONLY_TOOLS:
        return False
    if tool.startswith("telegram_get_"):
        return False
    return True


def _clip(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def side_effect_calls(audit: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        call
        for call in (audit or [])
        if isinstance(call, dict) and is_side_effect_tool(str(call.get("tool") or ""))
    ]


def audit_tool_result(call: dict[str, Any]) -> Any:
    result = call.get("result")
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


def cursor_finished_in_audit(audit: list[dict[str, Any]] | None) -> bool:
    for call in reversed(audit or []):
        tool = str(call.get("tool") or "")
        if tool not in {"cursorremote_check", "cursorremote_do"}:
            continue
        if call.get("status") != "success":
            return False
        payload = audit_tool_result(call)
        return isinstance(payload, dict) and bool(payload.get("done"))
    return False


def is_internal_execution(context: dict[str, Any] | None) -> bool:
    """True for scheduled/flush/tick runs — not a live customer Telegram turn."""
    ctx = context or {}
    if ctx.get("_intake_flush") or ctx.get("employee_tick"):
        return True
    return str(ctx.get("source") or "") in INTERNAL_EXECUTION_SOURCES


def _peer_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        try:
            return str(int(text))
        except ValueError:
            return text
    return text.lower().lstrip("@")


def peers_equal(left: Any, right: Any) -> bool:
    a = _peer_key(left)
    b = _peer_key(right)
    return bool(a) and a == b


def is_admin_peer(entity: Any, admin_ids: set[int] | None) -> bool:
    key = _peer_key(entity)
    if not key.lstrip("-").isdigit():
        return False
    try:
        return int(key) in (admin_ids or set())
    except ValueError:
        return False


def is_customer_origin_peer(
    entity: Any,
    context: dict[str, Any] | None,
    admin_ids: set[int] | None = None,
) -> bool:
    if entity in (None, "", False):
        return False
    if is_admin_peer(entity, admin_ids):
        return False
    ctx = context or {}
    for candidate in (
        ctx.get("reply_chat_id"),
        ctx.get("chat_id"),
        ctx.get("sender_id"),
        ctx.get("entity"),
    ):
        if peers_equal(entity, candidate):
            return True
    return False


def cursor_result_ready_for_customer(
    audit: list[dict[str, Any]] | None,
    *,
    cursor_was_in_flight: bool = False,
) -> bool:
    """True only when THIS assignment finished in Cursor — not a leftover idle summary."""
    saw_prompt_sent = False
    saw_in_progress = False
    last_success: dict[str, Any] | None = None
    for call in audit or []:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        if tool not in {"cursorremote_check", "cursorremote_do"}:
            continue
        if call.get("status") != "success":
            continue
        payload = audit_tool_result(call)
        if not isinstance(payload, dict):
            continue
        last_success = payload
        if payload.get("skipped_prompt"):
            continue
        if payload.get("prompt_sent"):
            saw_prompt_sent = True
        if payload.get("done") is False:
            saw_in_progress = True
    if not last_success or not last_success.get("done"):
        return False
    if last_success.get("skipped_prompt") and not (cursor_was_in_flight or saw_in_progress):
        return False
    return bool(saw_prompt_sent or saw_in_progress or cursor_was_in_flight)


def should_suppress_manager_status(
    context: dict[str, Any] | None,
    audit: list[dict[str, Any]] | None,
    work_item: Any = None,
) -> bool:
    """Keep routine Cursor-wait progress out of manager Telegram."""
    ctx = context or {}
    if not is_internal_execution(ctx) or work_item is None:
        return False
    status = str(getattr(work_item, "status", "") or "").strip()
    phase = str(getattr(work_item, "pm_phase", "") or "").strip()
    metadata = getattr(work_item, "metadata_json", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    waiting_for_cursor = status == "waiting_external" or bool(
        metadata.get("cursor_in_flight")
    )
    if not waiting_for_cursor:
        return False
    if status in {"failed", "waiting_manager"} or phase == "BLOCKED":
        return False
    for call in audit or []:
        if not isinstance(call, dict) or call.get("status") != "success":
            continue
        if str(call.get("tool") or "") in {"consult_manager", "request_approval"}:
            return False
    return not cursor_result_ready_for_customer(
        audit,
        cursor_was_in_flight=bool(ctx.get("_cursor_was_in_flight")),
    )


def pm_accept_succeeded(audit: list[dict[str, Any]] | None) -> bool:
    return any(
        isinstance(call, dict)
        and call.get("tool") == "pm_accept_task"
        and call.get("status") == "success"
        for call in audit or []
    )


def tracker_tool_closes_card(tool: str, arguments: dict[str, Any] | None) -> bool:
    name = str(tool or "").strip().lower()
    if name in {"complete_task", "complete_card"}:
        return True
    if name not in {
        "move_task",
        "move_card",
        "update_task",
        "update_card",
        "set_task_status",
    }:
        return False
    args = arguments if isinstance(arguments, dict) else {}
    status = str(
        args.get("status")
        or args.get("to_status")
        or args.get("new_status")
        or args.get("column")
        or ""
    ).strip().lower()
    return status in {"completed", "done", "closed", "cancelled", "canceled"}


def should_redirect_customer_outbound(
    context: dict[str, Any] | None,
    audit: list[dict[str, Any]] | None,
    entity: Any,
    *,
    admin_ids: set[int] | None = None,
) -> bool:
    if not is_internal_execution(context):
        return False
    if not is_customer_origin_peer(entity, context, admin_ids):
        return False
    if (context or {}).get("_pm_mode") and not pm_accept_succeeded(audit):
        return True
    return not cursor_result_ready_for_customer(
        audit,
        cursor_was_in_flight=bool((context or {}).get("_cursor_was_in_flight")),
    )


def format_manager_status(
    *,
    agent_name: str,
    text: str,
    work_item_id: Any = None,
    source: Any = None,
) -> str:
    labels = {
        "intake_flush": "Запуск накопленного задания",
        "scheduled": "Проверка работы",
        "employee_tick": "Тик сотрудника",
        "employee_heartbeat": "Сторож",
    }
    title = labels.get(str(source or "").strip(), "Служебный статус")
    lines = [f"[Ice.agent] {title} — «{agent_name}»"]
    if work_item_id not in (None, "", False):
        lines.append(f"Кейс #{work_item_id}")
    lines.append((text or "").strip())
    return "\n".join(lines)


def format_admin_action_report(
    *,
    agent_name: str,
    audit: list[dict[str, Any]] | None,
    user_message: str = "",
    chat_id: Any = None,
    sender_id: Any = None,
    sender_username: str | None = None,
    source: Any = None,
) -> str | None:
    """Build a Russian admin digest for mutating tool calls, or None if none."""
    src = str(source or "").strip()
    if src in {"scheduled", "employee_tick", "employee_heartbeat", "intake_flush"}:
        extra = [
            call
            for call in (audit or [])
            if isinstance(call, dict)
            and is_side_effect_tool(str(call.get("tool") or ""))
            and str(call.get("tool") or "") not in INTERNAL_FOLLOWUP_TOOLS
        ]
        if not extra:
            return None
    calls = side_effect_calls(audit)
    if not calls:
        return None
    who = []
    if sender_username:
        who.append(f"@{sender_username}")
    if sender_id not in (None, ""):
        who.append(f"id={sender_id}")
    lines = [
        f"[Ice.agent] Агент «{agent_name}» выполнил действия",
    ]
    if chat_id not in (None, ""):
        lines.append(f"Чат: {chat_id}")
    if who:
        lines.append(f"Собеседник: {', '.join(who)}")
    query = _clip(user_message, 280)
    if query:
        lines.append(f"Запрос: {query}")
    lines.append("Действия:")
    for call in calls:
        tool = call.get("tool") or "?"
        status = call.get("status") or "?"
        lines.append(f"• {tool} — {status}")
        args = call.get("arguments")
        if args:
            lines.append(f"  аргументы: {_clip(args, 320)}")
        if call.get("status") == "error":
            err = _clip(call.get("error"), 320)
            if err:
                lines.append(f"  ошибка: {err}")
        else:
            result = _clip(call.get("result"), 320)
            if result:
                lines.append(f"  результат: {result}")
    return "\n".join(lines)
