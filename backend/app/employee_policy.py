"""Configurable employee autonomy and approval policy."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .tools import APPROVAL_REQUIRED_TOOLS

APPROVAL_TOOL_LABELS: dict[str, str] = {
    "sip_dial": "Исходящие SIP-звонки",
    "telegram_delete_messages": "Удаление сообщений Telegram",
    "telegram_delete_dialog": "Удаление диалога Telegram",
    "telegram_leave_channel": "Выход из канала Telegram",
    "mcp_cursorremote_send_prompt": "CursorRemote: send_prompt",
    "mcp_cursorremote_approve": "CursorRemote: approve",
    "mcp_cursorremote_click_action": "CursorRemote: click_action",
    "mcp_cursorremote_new_chat": "CursorRemote: new_chat",
    "mcp_cursorremote_switch_window": "CursorRemote: switch_window",
}

DEFAULT_EMPLOYEE_POLICY: dict[str, Any] = {
    # Dangerous by default; sip_dial excluded — customer/manager orders should go through.
    "approval_required_tools": sorted(
        tool for tool in APPROVAL_REQUIRED_TOOLS if tool != "sip_dial"
    ),
    "manager_orders_without_approval": True,
    "customer_requests_without_approval": True,
    "consult_manager_on_idle_tick": False,
    "tick_instruction_extra": "",
}


def policy_catalog() -> dict[str, Any]:
    return {
        "approval_tool_options": [
            {"id": tool, "label": APPROVAL_TOOL_LABELS.get(tool, tool)}
            for tool in sorted(APPROVAL_REQUIRED_TOOLS)
        ],
        "defaults": deepcopy(DEFAULT_EMPLOYEE_POLICY),
    }


def employee_policy(profile: Any) -> dict[str, Any]:
    raw = getattr(profile, "config_json", None) or {}
    policy = raw.get("policy") if isinstance(raw, dict) else None
    if not isinstance(policy, dict):
        policy = {}
    merged = {**DEFAULT_EMPLOYEE_POLICY, **policy}
    tools = merged.get("approval_required_tools")
    if tools is None:
        merged["approval_required_tools"] = list(DEFAULT_EMPLOYEE_POLICY["approval_required_tools"])
    elif isinstance(tools, list):
        merged["approval_required_tools"] = [str(item) for item in tools]
    else:
        merged["approval_required_tools"] = list(DEFAULT_EMPLOYEE_POLICY["approval_required_tools"])
    return merged


def normalize_action_name(action_name: str) -> str:
    text = (action_name or "").strip()
    lower = text.lower()
    if not text:
        return text
    if lower in APPROVAL_REQUIRED_TOOLS:
        return lower
    if lower.startswith("mcp_") and lower in APPROVAL_REQUIRED_TOOLS:
        return lower
    if "sip_dial" in lower or "sip" in lower or "звон" in lower or "call" in lower:
        return "sip_dial"
    if "delete_messages" in lower or "удал" in lower and "сообщ" in lower:
        return "telegram_delete_messages"
    if "delete_dialog" in lower:
        return "telegram_delete_dialog"
    if "leave_channel" in lower:
        return "telegram_leave_channel"
    if "cursorremote" in lower and "send_prompt" in lower:
        return "mcp_cursorremote_send_prompt"
    if "cursorremote" in lower and "new_chat" in lower:
        return "mcp_cursorremote_new_chat"
    if "cursorremote" in lower and "switch_window" in lower:
        return "mcp_cursorremote_switch_window"
    return text


def action_matches_tool(action_name: str | None, tool_name: str) -> bool:
    if not action_name or not tool_name:
        return False
    normalized = normalize_action_name(action_name)
    return normalized.lower() == tool_name.lower()


def approval_required_for_tool(
    profile: Any,
    tool_name: str,
    context: dict[str, Any] | None = None,
) -> bool:
    policy = employee_policy(profile)
    required = set(policy.get("approval_required_tools") or [])
    if tool_name not in required:
        return False
    ctx = context or {}
    if policy.get("manager_orders_without_approval") and ctx.get("is_admin"):
        return False
    if policy.get("customer_requests_without_approval") and not ctx.get("is_admin"):
        if ctx.get("source") == "telegram" and not ctx.get("employee_tick"):
            return False
    if ctx.get("employee_tick") and tool_name == "sip_dial":
        return False
    return True


def build_employee_tick_instruction(profile: Any) -> str:
    policy = employee_policy(profile)
    parts = [
        "Ты автономный сотрудник. Это твой рабочий тик (heartbeat), не сообщение клиента.",
        "Просмотри миссию, планы (час/день/неделя/месяц), потребности и открытые консультации.",
        "Сделай один полезный шаг: обнови план, выполни шаг, сохрани заметки через self_configure.",
    ]
    if policy.get("consult_manager_on_idle_tick"):
        parts.append(
            "Если делать нечего — спроси руководителя через consult_manager "
            "(не request_approval без реальной опасной операции)."
        )
    else:
        parts.append(
            "Не создавай consult_manager и request_approval без срочной блокировки — "
            "зафиксируй статус в self_notes и заверши тик."
        )
    parts.append(
        "request_approval используй только для действительно опасных операций из списка политики. "
        "action_name передавай как имя инструмента (например sip_dial), не человекочитаемый текст."
    )
    parts.append("Финальный текст — краткий внутренний журнал тика, не сообщение клиенту.")
    extra = str(policy.get("tick_instruction_extra") or "").strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


def customer_telegram_instruction() -> str:
    return (
        "You are speaking with a customer in Telegram, not with your manager. "
        "Execute reasonable customer requests directly: answer questions, send follow-ups, "
        "place outbound calls when they ask to call or provide a phone number (use sip_dial). "
        "NEVER mention manager approval, internal consultations, request_approval, or platform "
        "mechanics to the customer. "
        "If they say 'call me' without a number, ask once naturally for their phone number. "
        "When they send a phone number, call immediately — do not wait for manager confirmation. "
        "Do not claim a call or message was sent unless the tool call succeeded."
    )


def manager_telegram_instruction() -> str:
    return (
        "You are speaking with your manager/administrator in Telegram. "
        "When they give a direct order (write to someone, call someone, gather requirements), "
        "execute it immediately using the appropriate tools. "
        "Do not use request_approval or consult_manager for routine operational tasks they explicitly requested. "
        "Use request_approval only for destructive or high-risk actions listed in your approval policy."
    )
