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
    if src in {"scheduled", "employee_tick", "employee_heartbeat"}:
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
