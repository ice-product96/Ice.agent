"""Approval and scope rails for development: verdicts from judges, not word lists.

``detect_approval`` verifies that a customer/manager message really approves the
subject (spec, cost, slice, development start) before the platform records a
confirmation that unlocks Cursor.

``judge_scope`` decides whether a structured task lies inside the confirmed spec and
how big / risky it is, replacing ``infer_inside_agreed_scope`` / ``infer_small_fix``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from .db import DecisionRecord, MessageLog, WorkItem
from .judgment import ApprovalVerdict, JudgmentResult, JudgmentService, ScopeVerdict

ApprovalSubject = Literal["spec", "cost", "slice", "development_start", "result", "other"]
APPROVAL_SUBJECTS: tuple[str, ...] = ("spec", "cost", "slice", "development_start", "result", "other")

INTERNAL_SOURCES = frozenset(
    {"employee_tick", "employee_heartbeat", "scheduled", "consult_resolved", "intake_flush", "cron"}
)


def actor_from_context(context: dict[str, Any] | None, confirmed_by: str = "") -> str:
    """Who is speaking: derived from the routing context, never from text markers."""
    ctx = context or {}
    if ctx.get("is_admin"):
        return "manager"
    source = str(ctx.get("source") or "")
    if source == "telegram" and (ctx.get("message_id") or ctx.get("client_id") or ctx.get("sender_id")):
        return "customer"
    if source in INTERNAL_SOURCES or not source:
        # Internal tick: the agent claims a decision without a live message.
        value = (confirmed_by or "").strip().lower()
        if value in {"customer", "client", "заказчик", "клиент"}:
            return "customer"
        if value in {"manager", "owner", "admin", "руководитель", "владелец"}:
            return "manager"
        return "unknown"
    return "customer"


def legacy_subject(topic: str, decision: str) -> ApprovalSubject:
    """Old keyword mapping, kept only for shadow comparison and off mode."""
    from .pm_state import topic_is_spec_approval
    from .project_schedule import topic_is_cost_approval

    if topic_is_spec_approval(topic, decision):
        return "spec"
    if topic_is_cost_approval(topic, decision):
        return "cost"
    return "other"


@dataclass
class ApprovalDecision:
    approved: bool
    subject: ApprovalSubject
    actor: str
    verdict: str
    reason: str
    conditions: list[str] = field(default_factory=list)
    legacy: bool = False
    judgment: JudgmentResult | None = None

    @property
    def customer_approved(self) -> bool:
        return self.approved and self.actor == "customer"

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "subject": self.subject,
            "actor": self.actor,
            "verdict": self.verdict,
            "reason": self.reason,
            "conditions": self.conditions,
            "legacy": self.legacy,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


async def _last_agent_message(db: Any, chat_id: Any, agent_id: int | None) -> str:
    if db is None or chat_id in (None, ""):
        return ""
    try:
        row = await db.scalar(
            select(MessageLog)
            .where(
                MessageLog.chat_id == str(chat_id),
                MessageLog.direction == "out",
                *( [MessageLog.agent_id == agent_id] if agent_id is not None else [] ),
            )
            .order_by(MessageLog.id.desc())
            .limit(1)
        )
    except Exception:
        return ""
    return str(getattr(row, "text", "") or "")[:2000]


def approval_payload(
    *,
    message: str,
    actor: str,
    subject: str,
    topic: str,
    decision: str,
    item: WorkItem | None,
    spec: dict[str, Any] | None,
    cost: dict[str, Any] | None,
    pending_question: str,
) -> dict[str, Any]:
    return {
        "message": str(message or "")[:6000],
        "sender_role": actor,
        "claimed_subject": subject,
        "agent_claim": {"topic": topic, "decision": decision},
        "pending_question_from_agent": pending_question,
        "task": (
            {
                "id": item.id,
                "title": item.title,
                "goal": str(item.goal or "")[:1500],
                "pm_phase": item.pm_phase,
                "requirements": [str(v) for v in list(item.requirements or [])][:20],
            }
            if item is not None
            else None
        ),
        "spec": (
            {
                "status": spec.get("status"),
                "summary": str(spec.get("summary") or "")[:2000],
                "in_scope": list(spec.get("in_scope") or [])[:30],
                "out_of_scope": list(spec.get("out_of_scope") or [])[:30],
            }
            if spec
            else None
        ),
        "cost": cost or None,
    }


async def detect_approval(
    db: Any,
    *,
    judgment: JudgmentService | None,
    context: dict[str, Any] | None,
    subject: str,
    topic: str,
    decision: str,
    confirmed_by: str,
    item: WorkItem | None,
    spec: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    project_config: dict[str, Any] | None = None,
    client: Any | None = None,
) -> ApprovalDecision:
    """Is the live message (or the agent's claim) a real approval of ``subject``?"""
    ctx = context or {}
    actor = actor_from_context(ctx, confirmed_by)
    claimed_subject: ApprovalSubject = (
        subject if subject in APPROVAL_SUBJECTS else legacy_subject(topic, decision)  # type: ignore[assignment]
    )
    service = judgment or JudgmentService(settings=None)
    mode = service.mode("approval_detect", project_config)
    message = str(ctx.get("_user_message") or "").strip()
    live_message = actor in {"customer", "manager"} and bool(message) and str(ctx.get("source") or "") not in INTERNAL_SOURCES

    legacy_approved = claimed_subject != "other"
    legacy = ApprovalDecision(
        approved=legacy_approved,
        subject=claimed_subject,
        actor=actor,
        verdict="legacy_claim" if legacy_approved else "legacy_not_approval",
        reason="Agent recorded the decision; legacy keyword/subject path",
        legacy=True,
    )
    if mode == "off" or (not service.configured and client is None) or not live_message:
        # Without a live message there is nothing to judge: the agent's explicit
        # subject is the claim, stored decisions carry it.
        return legacy

    result = await service.judge(
        "approval_detect",
        approval_payload(
            message=message,
            actor=actor,
            subject=claimed_subject,
            topic=topic,
            decision=decision,
            item=item,
            spec=spec,
            cost=cost,
            pending_question=await _last_agent_message(db, ctx.get("chat_id"), getattr(item, "agent_id", None)),
        ),
        db=db,
        agent_id=getattr(item, "agent_id", None),
        work_item_id=getattr(item, "id", None),
        chat_id=ctx.get("chat_id"),
        project_id=getattr(item, "project_id", None),
        project_config=project_config,
        legacy={"verdict": "approved" if legacy_approved else "not_an_approval"},
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, ApprovalVerdict) else None
    if mode == "shadow" or verdict is None:
        legacy.judgment = result
        if verdict is None and mode == "enforce":
            return ApprovalDecision(
                approved=False,
                subject=claimed_subject,
                actor=actor,
                verdict="degraded",
                reason=f"approval judge unavailable: {result.error or 'no verdict'}",
                judgment=result,
            )
        return legacy
    subject_out: ApprovalSubject = verdict.subject if verdict.subject in APPROVAL_SUBJECTS else claimed_subject  # type: ignore[assignment]
    approved = verdict.verdict == "approved" and result.confident
    if verdict.verdict == "partial" and result.confident:
        # Partial approval counts only for the approved part; the rail treats it as approval
        # with conditions the agent must record and honour.
        approved = True
    return ApprovalDecision(
        approved=approved,
        subject=subject_out,
        actor=actor if verdict.approved_by == "unknown" else verdict.approved_by,
        verdict=verdict.verdict if result.confident else "low_confidence",
        reason=verdict.reasoning or verdict.verdict,
        conditions=list(verdict.conditions or []),
        judgment=result,
    )


# --------------------------------------------------------------------------- scope


@dataclass
class ScopeDecision:
    inside_scope: bool
    small_fix: bool
    high_risk: bool
    ready: bool
    verdict: str
    reason: str
    outside_items: list[str] = field(default_factory=list)
    size: str = ""
    risk: str = ""
    legacy: bool = False
    judgment: JudgmentResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "inside_scope": self.inside_scope,
            "small_fix": self.small_fix,
            "high_risk": self.high_risk,
            "ready": self.ready,
            "verdict": self.verdict,
            "reason": self.reason,
            "outside_items": self.outside_items,
            "size": self.size,
            "risk": self.risk,
            "legacy": self.legacy,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


async def _recent_decisions(db: Any, item: WorkItem, limit: int = 12) -> list[dict[str, Any]]:
    if db is None or not item.project_id:
        return []
    rows = list(
        await db.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.project_id == item.project_id)
            .order_by(DecisionRecord.id.desc())
            .limit(limit)
        )
    )
    return [
        {
            "topic": row.topic,
            "decision": str(row.decision or "")[:400],
            "confirmed_by": row.confirmed_by,
            "work_item_id": row.work_item_id,
            "subject": (row.context_json or {}).get("subject"),
        }
        for row in rows
    ]


def scope_payload(item: WorkItem, spec: dict[str, Any], decisions: list[dict[str, Any]], autonomy_level: str) -> dict[str, Any]:
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    return {
        "task": {
            "id": item.id,
            "title": item.title,
            "type": item.task_type,
            "priority": item.priority,
            "goal": str(item.goal or "")[:2000],
            "requirements": [str(v) for v in list(item.requirements or [])][:40],
            "acceptance_criteria": [str(v) for v in list(item.acceptance_criteria or [])][:40],
            "constraints": [str(v) for v in list(item.constraints or [])][:20],
            "edge_cases": [str(v) for v in list(item.edge_cases or [])][:20],
            "estimated_duration_minutes": ctx.get("estimated_duration_minutes"),
            "business_reason": str(ctx.get("business_reason") or "")[:800],
            "tracker_sourced": bool(ctx.get("tracker_task_id")),
        },
        "spec": {
            "status": spec.get("status"),
            "summary": str(spec.get("summary") or "")[:3000],
            "goals": list(spec.get("goals") or [])[:30],
            "in_scope": list(spec.get("in_scope") or [])[:60],
            "out_of_scope": list(spec.get("out_of_scope") or [])[:60],
            "constraints": list(spec.get("constraints") or [])[:30],
            "modules": list(spec.get("modules") or [])[:40],
        },
        "recorded_decisions": decisions,
        "project_autonomy_level": autonomy_level,
    }


async def judge_scope(
    db: Any,
    item: WorkItem,
    *,
    spec: dict[str, Any],
    autonomy_level: str,
    judgment: JudgmentService | None,
    client_confirmed: bool,
    project_config: dict[str, Any] | None = None,
    client: Any | None = None,
) -> ScopeDecision:
    """Scope, size and risk of a task versus the confirmed spec."""
    from .pm_state import infer_inside_agreed_scope, infer_small_fix

    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    legacy_inside = infer_inside_agreed_scope(item, client_confirmed=client_confirmed)
    legacy_small = infer_small_fix(item)
    legacy = ScopeDecision(
        inside_scope=legacy_inside,
        small_fix=legacy_small,
        high_risk=bool(ctx.get("high_risk")),
        ready=True,
        verdict="legacy_inside" if legacy_inside else "legacy_outside",
        reason="Legacy flags from execution verdict / estimate thresholds",
        legacy=True,
    )
    service = judgment or JudgmentService(settings=None)
    mode = service.mode("scope_judge", project_config)
    if mode == "off" or (not service.configured and client is None):
        return legacy
    decisions = await _recent_decisions(db, item)
    result = await service.judge(
        "scope_judge",
        scope_payload(item, spec, decisions, autonomy_level),
        db=db,
        agent_id=item.agent_id,
        work_item_id=item.id,
        chat_id=item.chat_id,
        project_id=item.project_id,
        project_config=project_config,
        legacy={"verdict": "inside_spec" if legacy_inside else "outside_spec"},
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, ScopeVerdict) else None
    if mode == "shadow":
        legacy.judgment = result
        return legacy
    if verdict is None:
        return ScopeDecision(
            inside_scope=False,
            small_fix=False,
            high_risk=True,
            ready=False,
            verdict="degraded",
            reason=f"scope judge unavailable: {result.error or 'no verdict'}",
            judgment=result,
        )
    inside = verdict.verdict == "inside_spec" and result.confident
    high_risk = verdict.risk == "high" or bool(ctx.get("high_risk"))
    small_fix = verdict.size in {"trivial", "small"} and not high_risk
    return ScopeDecision(
        inside_scope=inside,
        small_fix=small_fix,
        high_risk=high_risk,
        ready=bool(verdict.ready_to_execute),
        verdict=verdict.verdict if result.confident else "low_confidence",
        reason=verdict.reasoning or verdict.verdict,
        outside_items=list(verdict.outside_items or []),
        size=verdict.size,
        risk=verdict.risk,
        judgment=result,
    )


def scope_block_message(decision: ScopeDecision, *, client_confirmed: bool) -> str | None:
    """Text for the PermissionError raised by the Cursor rail, or None when allowed."""
    if decision.legacy:
        return None
    if decision.verdict == "degraded":
        return (
            f"Development is blocked: {decision.reason}. Do not send anything to Cursor; "
            "retry on the next tick or consult_manager if it persists."
        )
    if not decision.ready:
        return (
            "Task is not concrete enough for an engineer: "
            + (decision.reason or "goal/requirements/acceptance criteria are vague")
            + ". Refine it with pm_structure_task or ask the customer the minimum missing questions."
        )
    if decision.verdict == "low_confidence":
        return (
            "Scope is ambiguous versus the confirmed spec: " + decision.reason
            + ". Ask the customer to confirm this exact slice, then pm_record_decision(subject='slice')."
        )
    if decision.verdict in {"outside_spec", "spec_missing"} and not client_confirmed:
        items = "; ".join(decision.outside_items[:6]) or decision.reason
        return (
            f"Request is outside the confirmed spec: {items}. Agree it with the customer "
            "(pm_update_spec + pm_record_decision subject='spec' or 'slice') before Cursor."
        )
    if decision.verdict == "partially_inside" and not client_confirmed:
        items = "; ".join(decision.outside_items[:6]) or decision.reason
        return (
            f"Part of the request is outside the confirmed spec: {items}. Either split those items "
            "into a separate case (pm_structure_task create_new_task=true) or get the customer's "
            "explicit confirmation for this slice."
        )
    return None
