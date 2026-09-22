"""Intake judges: what an inbound message is, and who a manager reply answers.

Replaces the ``_ACK_WORDS`` / ``_WIPE_VERBS`` word lists and the reply-prefix
heuristic. Legacy heuristics stay callable for shadow comparison only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import WorkItem
from .judgment import IntentVerdict, JudgmentResult, JudgmentService, ManagerReplyVerdict

WORK_INTENTS = frozenset({"work_request", "change_request", "bug_report", "cancel"})


@dataclass
class IntentDecision:
    intent: str
    is_work: bool
    continues_open_case: bool
    is_fragment: bool
    operational_admin: bool
    wipe_scope: str | None
    summary: str
    legacy: bool = False
    judgment: JudgmentResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "is_work": self.is_work,
            "continues_open_case": self.continues_open_case,
            "is_fragment": self.is_fragment,
            "operational_admin": self.operational_admin,
            "wipe_scope": self.wipe_scope,
            "summary": self.summary,
            "legacy": self.legacy,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


def _case_brief(item: WorkItem | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "title": item.title,
        "goal": str(item.goal or "")[:600],
        "status": item.status,
        "pm_phase": item.pm_phase,
        "next_action": str(item.next_action or "")[:300],
        "updated_at": item.updated_at.isoformat() if getattr(item, "updated_at", None) else None,
    }


def legacy_intent(message: str, *, is_admin: bool) -> IntentDecision:
    from .work_items import is_operational_admin_command, looks_like_non_work_reply

    operational = is_operational_admin_command(message, is_admin=is_admin)
    non_work = looks_like_non_work_reply(message)
    if operational:
        intent = "operational_admin"
    elif non_work:
        intent = "acknowledgement"
    else:
        intent = "work_request"
    return IntentDecision(
        intent=intent,
        is_work=not non_work and not operational,
        continues_open_case=False,
        is_fragment=False,
        operational_admin=operational,
        wipe_scope="all" if operational else None,
        summary="",
        legacy=True,
    )


def intent_payload(
    *,
    message: str,
    context: dict[str, Any],
    open_case: WorkItem | None,
    recent_case: WorkItem | None,
    last_agent_message: str,
) -> dict[str, Any]:
    return {
        "message": str(message or "")[:6000],
        "sender_role": "manager" if context.get("is_admin") else "customer",
        "channel": str(context.get("source") or ""),
        "has_attachments": bool(context.get("_attachments") or context.get("attachments")),
        "open_case": _case_brief(open_case),
        "recent_closed_case": _case_brief(recent_case) if recent_case is not None and recent_case is not open_case else None,
        "last_agent_message": str(last_agent_message or "")[:2000],
    }


async def classify_message(
    db: Any,
    *,
    judgment: JudgmentService | None,
    message: str,
    context: dict[str, Any],
    open_case: WorkItem | None = None,
    recent_case: WorkItem | None = None,
    last_agent_message: str = "",
    agent_id: int | None = None,
    project_config: dict[str, Any] | None = None,
    client: Any | None = None,
) -> IntentDecision:
    """Classify one inbound message. Degraded judge → treat as work (never drop a customer)."""
    is_admin = bool(context.get("is_admin"))
    legacy = legacy_intent(message, is_admin=is_admin)
    service = judgment or JudgmentService(settings=None)
    mode = service.mode("message_intent", project_config)
    if mode == "off" or (not service.configured and client is None):
        return legacy
    if not str(message or "").strip():
        return legacy
    result = await service.judge(
        "message_intent",
        intent_payload(
            message=message,
            context=context,
            open_case=open_case,
            recent_case=recent_case,
            last_agent_message=last_agent_message,
        ),
        db=db,
        agent_id=agent_id,
        chat_id=context.get("chat_id"),
        project_id=context.get("project_id"),
        project_config=project_config,
        legacy={"verdict": legacy.intent},
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, IntentVerdict) else None
    if mode == "shadow" or verdict is None:
        legacy.judgment = result
        if verdict is None and mode == "enforce":
            # Fail open towards work: a dropped customer request is the expensive error.
            legacy.is_work = True
            legacy.operational_admin = False
            legacy.intent = "work_request"
            legacy.summary = "judge unavailable; treated as work"
        return legacy
    operational = verdict.verdict == "operational_admin" and is_admin and result.confident
    is_work = bool(verdict.is_work) or verdict.verdict in WORK_INTENTS
    if not result.confident and not is_work:
        # Unsure whether it is work: keep the customer, let the agent decide in context.
        is_work = True
    return IntentDecision(
        intent=verdict.verdict,
        is_work=is_work and not operational,
        continues_open_case=bool(verdict.continues_open_case),
        is_fragment=verdict.verdict == "fragment" and result.confident,
        operational_admin=operational,
        wipe_scope=verdict.wipe_scope if operational else None,
        summary=verdict.summary,
        judgment=result,
    )


# --------------------------------------------------------------------------- manager reply


@dataclass
class ManagerReplyDecision:
    status: str  # answered|approved|rejected
    answer: str
    verdict: str
    legacy: bool = False
    judgment: JudgmentResult | None = None


def legacy_manager_reply(requires_approval: bool, text: str) -> str:
    from .routing import consultation_status_for_reply

    return consultation_status_for_reply(requires_approval, text)


async def classify_manager_reply(
    db: Any,
    *,
    judgment: JudgmentService | None,
    requires_approval: bool,
    question: str,
    action_name: str | None,
    text: str,
    agent_id: int | None = None,
    work_item_id: int | None = None,
    client: Any | None = None,
) -> ManagerReplyDecision:
    """approved / rejected / answered from free-form manager text."""
    legacy_status = legacy_manager_reply(requires_approval, text)
    legacy = ManagerReplyDecision(status=legacy_status, answer=str(text or "").strip(), verdict=legacy_status, legacy=True)
    service = judgment or JudgmentService(settings=None)
    mode = service.mode("manager_reply")
    if mode == "off" or (not service.configured and client is None) or not str(text or "").strip():
        return legacy
    result = await service.judge(
        "manager_reply",
        {
            "consultation_question": str(question or "")[:4000],
            "requires_approval": bool(requires_approval),
            "action_to_unlock": action_name,
            "manager_reply": str(text or "")[:4000],
        },
        db=db,
        agent_id=agent_id,
        work_item_id=work_item_id,
        legacy={"verdict": legacy_status},
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, ManagerReplyVerdict) else None
    if mode == "shadow":
        legacy.judgment = result
        return legacy
    if verdict is None:
        # Degraded: never unlock a dangerous action on a guess.
        return ManagerReplyDecision(
            status="answered" if requires_approval else legacy_status,
            answer=str(text or "").strip(),
            verdict="degraded",
            judgment=result,
        )
    if requires_approval:
        if verdict.verdict == "approved" and result.confident:
            status = "approved"
        elif verdict.verdict == "rejected" and result.confident:
            status = "rejected"
        else:
            status = "answered"
    else:
        status = "answered"
    return ManagerReplyDecision(
        status=status,
        answer=verdict.answer or str(text or "").strip(),
        verdict=verdict.verdict if result.confident else "low_confidence",
        judgment=result,
    )
