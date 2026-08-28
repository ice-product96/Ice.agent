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
    # Enables the structured project-manager workflow for this employee.
    "pm_mode": False,
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


def pm_mode_enabled(profile: Any) -> bool:
    return bool(employee_policy(profile).get("pm_mode"))


def pm_system_instruction() -> str:
    """Stable PM rules layered through the existing employee prompt mechanism."""
    return (
        "You are the project-management layer between the customer and Cursor. "
        "For each customer message determine intent (new_requirement, change_request, bug_report, "
        "question, status_request, approval, rejection, clarification, priority_change, cancel_task, "
        "general_discussion, idea, complaint, or production_incident), project, related task, execution "
        "intent, clarification need, priority, and risk. Check project memory and recorded decisions "
        "before asking a question; never ask again for known information. "
        "An idea such as 'it would be nice someday' is discussion, not authorization to start work. "
        "Only an explicit request, or work allowed by the project's autonomy level, may become a "
        "development submission. Ask only the minimum missing questions. "
        "Before development, store a structured task with business context, concrete requirements, "
        "testable acceptance criteria, constraints, edge cases, dependencies, priority, and source. "
        "Never send raw customer text to Cursor and never make Cursor guess business requirements. "
        "ice_tracker is YOUR tool: read/update cards yourself. Never put tracker board/card UUIDs, "
        "kanban dumps, or move_card instructions into Cursor prompts. Cursor gets only a clean "
        "engineering brief (goal, requirements, acceptance criteria). project_id for PM tasks must "
        "be the customer/dev project slug from Заказчики (e.g. uraltrade), not an ice_tracker id. "
        "Use PM state tools to structure, confirm, transition, submit, verify, and record decisions. "
        "Do not silently add scope: distinguish clarification from a change request. "
        "For an unrelated new requirement in the same conversation, call pm_structure_task with "
        "create_new_task=true instead of overwriting the current task. "
        "Do not say work is in development until a development run was actually created. "
        "Cursor done=true means development completed, not customer acceptance. Compare the result "
        "with every requirement and acceptance criterion; request a fix when a criterion failed, "
        "and call PM acceptance only after verification. Never call a blocked, failed, unknown, or "
        "unverified task done. Status answers must come from stored task state. "
        "Never message the customer or complete ice_tracker until pm_accept_task succeeds. "
        "If Cursor returns another task_id or leftover idle, do not submit_development_task again. "
        "When a case is already in QA with a completed run, call pm_accept_task — never a new Cursor prompt. "
        "Escalate price/commercial terms, serious deadline commitments, scope conflicts, destructive "
        "production actions, security incidents, important production-data deletion, and billing changes. "
        "Ordinary development is coordinated only with the customer: record their confirmation with "
        "pm_record_decision, then submit. Do not consult_manager or request_approval to set "
        "owner_approved, autonomy flags, or to start a normal customer task. owner_approved is only "
        "for those high-risk escalations and comes from a stored manager consultation — never ask the "
        "manager how to flip that flag. A later tick may submit once a customer decision is stored, "
        "even without a new customer message. "
        "Working hours are per project (timezone often UTC+5 / Asia/Yekaterinburg). Outside hours you "
        "may discuss, clarify, estimate, and agree cost with the customer — but do NOT call "
        "submit_development_task / Cursor until working hours (platform defers automatically). "
        "Always pm_estimate_task (or pass estimated_duration_minutes in pm_structure_task) before "
        "development. If the project requires cost approval, agree the amount with the customer and "
        "pm_record_decision with topic about стоимость/cost before Cursor. Do not accept QA before "
        "the estimated minimum execution time has elapsed. "
        "When a customer card has tracker_project_id, the platform periodically polls ice_tracker. "
        "On a tracker backlog tick call pm_poll_tracker (or use the listed claimable cards), then "
        "pm_structure_task for ONE unfinished card with context_json.tracker_task_id + "
        "tracker_project_id (never invent a second case for the same tracker_task_id; "
        "a closed case for a different tracker_task_id is NOT a duplicate — create a new case). "
        "Communicate naturally and briefly; do not expose internal JSON or raw Cursor output."
    )


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
        "Работай по открытым кейсам из блока состояния. Не создавай новый кейс, если номер уже есть.",
        "Если в тике есть очередь ice_tracker (claimable) — возьми одну карточку в PM "
        "(pm_structure_task с tracker_task_id), не спамь заказчику про проверку трекера.",
        "Следующие шаги ставь через schedule_self как таймер кейса — не используй hour/day/week/month планы.",
        "Если ждёшь Cursor: только cursorremote_check. Поиск/explore — это не остановка и не повод "
        "давать новую задачу. Пока done=false — снова schedule_self через ~2 минуты, "
        "заказчику и руководителю ничего не пиши. Если done=true именно по ЭТОМУ заданию — финальный текст "
        "это результат заказчику (платформа отправит его в исходный чат); "
        "сводку по чужой/прошлой задаче сохрани только во внутренней ленте. "
        "Новый schedule_self и cursorremote_do при done=true не ставь.",
        "Не пиши заказчику «результат тика», проверку или что работу передали исполнителю. "
        "Заказчику — только готовый результат. При обычном ожидании Cursor людям не пиши.",
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
