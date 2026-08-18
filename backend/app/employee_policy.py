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
    # Quiet window after a customer Telegram message before Cursor/execution.
    "intake_debounce_minutes": 15,
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
    try:
        merged["intake_debounce_minutes"] = max(
            0, min(180, int(merged.get("intake_debounce_minutes", 15)))
        )
    except (TypeError, ValueError):
        merged["intake_debounce_minutes"] = 15
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
    return text


def action_matches_tool(action_name: str | None, tool_name: str) -> bool:
    if not action_name or not tool_name:
        return False
    normalized = normalize_action_name(action_name)
    return normalized.lower() == tool_name.lower()


def intake_debounce_minutes(profile: Any) -> int:
    return int(employee_policy(profile).get("intake_debounce_minutes") or 0)


def customer_intake_instruction() -> str:
    return (
        "The customer may send several Telegram messages to complete one assignment. "
        "Reply NOW: acknowledge, answer questions, ask a short clarification if needed. "
        "Do NOT mention a delay, timer, queue, waiting period, or that work starts later. "
        "Do NOT call cursorremote_do, cursorremote_check, or give Cursor a job yet. "
        "Do NOT claim that Cursor already started or finished. "
        "Be natural and helpful. The platform will start execution after a quiet period."
    )


def customer_result_only_instruction() -> str:
    return (
        "The customer must receive ONLY the finished result of this assignment. "
        "Do NOT write the customer progress, status, rechecking, 'handed to Cursor', "
        "previous-task summaries, or that an executor returned an intermediate report. "
        "Those service notes are for the manager — the platform delivers them. "
        "Do NOT call telegram_send_message or telegram_send_file to the customer chat "
        "until cursorremote_do/check returns done=true for THIS assignment. "
        "A leftover idle summary from a previous Cursor job is not this result. "
        "If the customer sent images, include how Cursor should use them; the platform "
        "copies those files into the project automatically. "
        "If done=false, schedule_self to check again and write nothing to the customer."
    )


def customer_intake_flush_instruction() -> str:
    return (
        "The quiet period ended. The user message is the accumulated customer assignment. "
        "Execute it now with a SINGLE cursorremote_do that covers the whole brief. "
        "Do not send a separate Cursor job for each bullet, message, or image. "
        "If it is only small talk or already answered, do not start Cursor — finish without a new job. "
        "Do NOT mention that you waited, buffered messages, or that a timer fired. "
        "If the customer sent images, they are attached here and the platform will copy them "
        "into the Cursor project when you call cursorremote_do — describe how to use them. "
        + customer_result_only_instruction()
    )


def approval_required_for_tool(
    profile: Any,
    tool_name: str,
    context: dict[str, Any] | None = None,
) -> bool:
    policy = employee_policy(profile)
    required = set(policy.get("approval_required_tools") or [])
    if tool_name not in required:
        return False
    if "cursorremote" in tool_name.lower():
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
        "Ты автономный сотрудник. Это сторожевой тик, не живой чат с заказчиком.",
        "Работай только по открытым кейсам из блока состояния. Не создавай новый кейс, если номер уже есть.",
        "Следующие шаги ставь через schedule_self как таймер кейса — не используй hour/day/week/month планы.",
        "Если ждёшь Cursor: только cursorremote_check. Поиск/explore — это не остановка и не повод "
        "давать новую задачу. Пока done=false — снова schedule_self через ~2 минуты, "
        "заказчику ничего не пиши. Если done=true именно по ЭТОМУ заданию — финальный текст "
        "это результат заказчику (платформа отправит его в исходный чат); "
        "промежуточный журнал и сводку по чужой/прошлой задаче пиши руководителю, не заказчику. "
        "Новый schedule_self и cursorremote_do при done=true не ставь.",
        "Не пиши заказчику «результат тика», проверку или что работу передали исполнителю. "
        "Заказчику — только готовый результат. Служебный статус — руководителю.",
        "Сделай один полезный шаг по кейсу и при необходимости сохрани заметки через self_configure.",
    ]
    if policy.get("consult_manager_on_idle_tick"):
        parts.append(
            "Если открытых кейсов нет и делать нечего — спроси руководителя через consult_manager "
            "(не request_approval без реальной опасной операции)."
        )
    else:
        parts.append(
            "Не создавай consult_manager и request_approval без срочной блокировки. "
            "Если кейсов нет — заверши тик без сообщений людям."
        )
    parts.append(
        "request_approval используй только для действительно опасных операций из списка политики. "
        "action_name передавай как имя инструмента (например sip_dial), не человекочитаемый текст."
    )
    parts.append(
        "Финальный текст — внутренний журнал для UI, не сообщение клиенту. "
        "Исключение: Cursor done=true — тогда финальный текст для заказчика."
    )
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
        "NEVER tell the customer to click Allow/Accept in Cursor — you click those yourself "
        "via cursorremote_do / cursorremote_check. "
        "A successful send_prompt or done=false is NOT completion. Wait until cursorremote_do/"
        "cursorremote_check returns done=true, then message the customer with the result. "
        "If done=true, do not schedule_self again — even if summary is empty, report that the work is complete. "
        "If Cursor is still running (done=false), including search/explore, schedule_self in ~2 minutes to check again. "
        "Never send a second cursorremote_do for the same task because it 'looks stuck on search'."
        "If they say 'call me' without a number, ask once naturally for their phone number. "
                    "When they send a phone number, call immediately — do not wait for manager confirmation. "
                    "When you call sip_dial, always fill purpose (why you call, what to achieve) and opening. "
                    "After sip_dial returns ok=true, do not send a Telegram message about the call "
                    "(no «соединение установлено», «он ответил», «звоню»). The phone call is the response. "
                    "Do not claim a call or message was sent unless the tool call succeeded."
    )


def manager_telegram_instruction() -> str:
    return (
        "You are speaking with your manager/administrator in Telegram. "
        "When they give a direct order (write to someone, call someone, gather requirements), "
        "execute it immediately using the appropriate tools. "
        "Do not use request_approval or consult_manager for routine operational tasks they explicitly requested. "
        "Cursor Allow/Accept/Run dialogs: click them yourself with cursorremote_do / cursorremote_check. "
        "Never ask a human to press Allow in the IDE. "
        "done=true after cursorremote_check/do means Cursor finished — message the requester and do not reschedule. "
        "done=false means keep waiting with schedule_self in ~2 minutes. "
        "Use request_approval only for destructive or high-risk actions listed in your approval policy."
    )
