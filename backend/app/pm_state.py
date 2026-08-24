"""Deterministic project-manager state and persistence helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import CursorRun, DecisionRecord, ProjectState, WorkItem, WorkItemEvent, utcnow

logger = logging.getLogger(__name__)

PM_PHASES = (
    "DISCUSSION",
    "CLARIFICATION",
    "REQUIREMENTS_READY",
    "CLIENT_CONFIRMED",
    "READY_FOR_DEV",
    "IN_DEVELOPMENT",
    "BLOCKED",
    "DEV_COMPLETE",
    "QA",
    "CLIENT_REVIEW",
    "CHANGES_REQUESTED",
    "DONE",
    "CANCELLED",
)
CANONICAL_PHASES = PM_PHASES

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "DISCUSSION": frozenset({"CLARIFICATION", "REQUIREMENTS_READY", "CANCELLED"}),
    "CLARIFICATION": frozenset({"REQUIREMENTS_READY", "BLOCKED", "CANCELLED"}),
    "REQUIREMENTS_READY": frozenset(
        {"CLARIFICATION", "CLIENT_CONFIRMED", "READY_FOR_DEV", "CANCELLED"}
    ),
    "CLIENT_CONFIRMED": frozenset({"READY_FOR_DEV", "CHANGES_REQUESTED", "CANCELLED"}),
    "READY_FOR_DEV": frozenset(
        {
            "IN_DEVELOPMENT",
            "CLARIFICATION",
            "CHANGES_REQUESTED",
            "BLOCKED",
            "CANCELLED",
        }
    ),
    "IN_DEVELOPMENT": frozenset(
        {"DEV_COMPLETE", "CHANGES_REQUESTED", "BLOCKED", "CANCELLED"}
    ),
    "BLOCKED": frozenset(
        {
            "CLARIFICATION",
            "CHANGES_REQUESTED",
            "READY_FOR_DEV",
            "IN_DEVELOPMENT",
            "CANCELLED",
        }
    ),
    "DEV_COMPLETE": frozenset(
        {"QA", "BLOCKED", "CHANGES_REQUESTED", "CANCELLED"}
    ),
    "QA": frozenset(
        {"CLIENT_REVIEW", "DONE", "CHANGES_REQUESTED", "BLOCKED", "CANCELLED"}
    ),
    "CLIENT_REVIEW": frozenset({"DONE", "CHANGES_REQUESTED", "CANCELLED"}),
    "CHANGES_REQUESTED": frozenset(
        {
            "CLARIFICATION",
            "REQUIREMENTS_READY",
            "READY_FOR_DEV",
            "IN_DEVELOPMENT",
            "CANCELLED",
        }
    ),
    "DONE": frozenset(),
    "CANCELLED": frozenset(),
}

AUTONOMY_LEVELS = ("LEVEL_0", "LEVEL_1", "LEVEL_2", "LEVEL_3")
DEFAULT_AUTONOMY_LEVEL = "LEVEL_1"

CURSOR_RUN_TERMINAL_STATUSES = frozenset({"completed", "blocked", "failed", "cancelled"})


class TaskContract(BaseModel):
    task_id: str | None = None
    project_id: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern=r"^(feature|bug|change|technical)$")
    title: str = Field(min_length=1, max_length=300)
    context: dict[str, Any] = Field(default_factory=dict)
    requirements: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    priority: str = Field(default="normal", pattern=r"^(critical|high|normal|low)$")
    dependencies: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "requirements",
        "acceptance_criteria",
        "constraints",
        "edge_cases",
        "dependencies",
        "related_tasks",
    )
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        return [str(value).strip() for value in values if str(value).strip()]


class InvalidPhaseTransition(ValueError):
    """Raised when a PM phase transition is not in the state machine."""


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_key(*parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def can_transition(from_phase: str, to_phase: str) -> bool:
    return to_phase in ALLOWED_TRANSITIONS.get(from_phase, frozenset())


def validate_transition(from_phase: str, to_phase: str) -> None:
    if from_phase not in PM_PHASES:
        raise InvalidPhaseTransition(f"Unknown PM phase: {from_phase}")
    if to_phase not in PM_PHASES:
        raise InvalidPhaseTransition(f"Unknown PM phase: {to_phase}")
    if not can_transition(from_phase, to_phase):
        raise InvalidPhaseTransition(f"PM phase cannot transition from {from_phase} to {to_phase}")


def readiness_issues(item: WorkItem) -> list[str]:
    issues: list[str] = []
    if not str(item.goal or "").strip():
        issues.append("Missing goal")
    if not list(item.requirements or []):
        issues.append("Missing requirements")
    if not list(item.acceptance_criteria or []):
        issues.append("Missing acceptance criteria")
    if str(item.task_type or "") not in {"feature", "bug", "change", "technical"}:
        issues.append("Missing task type")
    if str(item.priority or "") not in {"critical", "high", "normal", "low"}:
        issues.append("Invalid priority")
    return issues


def validate_task(item: WorkItem) -> list[str]:
    """Return deterministic validation issues; an empty list means ready."""
    return readiness_issues(item)


def is_task_ready(item: WorkItem) -> bool:
    return not readiness_issues(item)


task_is_ready = is_task_ready


def normalize_autonomy_level(level: str | int) -> str:
    if isinstance(level, int):
        normalized = f"LEVEL_{level}"
    else:
        normalized = str(level).strip().upper().replace("-", "_")
        if normalized.isdigit():
            normalized = f"LEVEL_{normalized}"
    if normalized not in AUTONOMY_LEVELS:
        raise ValueError(f"Unknown autonomy level: {level}")
    return normalized


def autonomy_allows(current_level: str | int, required_level: str | int) -> bool:
    current = AUTONOMY_LEVELS.index(normalize_autonomy_level(current_level))
    required = AUTONOMY_LEVELS.index(normalize_autonomy_level(required_level))
    return current >= required


def autonomy_gate(level: str | int, action: str) -> bool:
    required_by_action = {
        "observe": "LEVEL_0",
        "plan": "LEVEL_0",
        "small_bug_fix": "LEVEL_1",
        "agreed_scope": "LEVEL_2",
        "ordinary_development": "LEVEL_3",
    }
    if action not in required_by_action:
        raise ValueError(f"Unknown autonomy action: {action}")
    return autonomy_allows(level, required_by_action[action])


def requires_approval(level: str | int, action: str) -> bool:
    return not autonomy_gate(level, action)


def submission_requires_approval(
    level: str | int,
    *,
    task_type: str,
    client_confirmed: bool,
    inside_agreed_scope: bool,
    small_fix: bool = False,
    high_risk: bool = False,
) -> bool:
    normalized = normalize_autonomy_level(level)
    if high_risk or normalized == "LEVEL_0":
        return True
    if normalized == "LEVEL_1":
        return task_type != "bug" or not inside_agreed_scope or not small_fix
    if normalized == "LEVEL_2":
        return not inside_agreed_scope and not client_confirmed
    return not inside_agreed_scope and not client_confirmed


def apply_task_contract(item: WorkItem, contract: TaskContract) -> WorkItem:
    item.project_id = contract.project_id
    item.task_type = contract.type
    item.title = contract.title
    item.context_json = contract.context
    item.goal = str(contract.context.get("business_reason") or contract.title)
    item.requirements = contract.requirements
    item.acceptance_criteria = contract.acceptance_criteria
    item.constraints = contract.constraints
    item.edge_cases = contract.edge_cases
    item.priority = contract.priority
    item.source_message_id = str(contract.source.get("message_id") or "") or None
    metadata = dict(item.metadata_json or {})
    metadata["pm"] = {
        "dependencies": contract.dependencies,
        "related_tasks": contract.related_tasks,
        "source": contract.source,
    }
    item.metadata_json = metadata
    return item


def _lines(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def render_task_brief(item: WorkItem) -> str:
    title = str(item.title or "").strip() or f"Work item {item.id}"
    task_type = str(item.task_type or "task")
    priority = str(item.priority or "normal")
    sections = [
        f"# Task: {title}",
        f"**Type:** {task_type}",
        f"**Priority:** {priority}",
        f"**Project:** {item.project_id or 'unspecified'}",
        f"## Goal\n{str(item.goal or '').strip()}",
        f"## Requirements\n{_lines(list(item.requirements or []))}",
        f"## Acceptance criteria\n{_lines(list(item.acceptance_criteria or []))}",
    ]
    constraints = _lines(list(item.constraints or []))
    edge_cases = _lines(list(item.edge_cases or []))
    if constraints:
        sections.append(f"## Constraints\n{constraints}")
    if edge_cases:
        sections.append(f"## Edge cases\n{edge_cases}")
    if item.context_json:
        context = json.dumps(item.context_json, ensure_ascii=False, sort_keys=True, indent=2)
        sections.append(f"## Context\n```json\n{context}\n```")
    return "\n\n".join(sections).strip() + "\n"


task_brief = render_task_brief


def parse_cursor_result(result: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, Mapping):
        parsed = dict(result)
    else:
        if not isinstance(result, str):
            raise ValueError("Cursor result must be a JSON object or JSON string")
        text = result.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        candidate = fenced.group(1) if fenced else text
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            raise ValueError("Cursor result must be a structured JSON object")
        else:
            if not isinstance(value, dict):
                raise ValueError("Cursor result JSON must be an object")
            parsed = value

    status = str(parsed.get("status", "")).strip().lower()
    status_aliases = {"success": "completed", "succeeded": "completed", "error": "failed"}
    status = status_aliases.get(status, status)
    if status not in CURSOR_RUN_TERMINAL_STATUSES:
        raise ValueError(f"Unknown Cursor result status: {status}")
    parsed["status"] = status
    implementation = parsed.get("implementation")
    verification = parsed.get("verification")
    if status == "completed" and (
        not isinstance(implementation, Mapping) or not isinstance(verification, Mapping)
    ):
        raise ValueError("Completed Cursor result requires implementation and verification")
    parsed["implementation"] = dict(implementation or {})
    parsed["verification"] = dict(verification or {})
    if status == "completed" and not isinstance(
        parsed["verification"].get("acceptance_criteria"),
        list,
    ):
        raise ValueError(
            "Completed Cursor result requires criterion-by-criterion verification"
        )
    for key in ("questions", "risks", "limitations"):
        value = parsed.get(key, [])
        parsed[key] = value if isinstance(value, list) else [value]
    return parsed


parse_result = parse_cursor_result


async def get_or_create_project_state(
    db: AsyncSession,
    project_id: str,
    *,
    autonomy_level: str | int = DEFAULT_AUTONOMY_LEVEL,
    config: Mapping[str, Any] | None = None,
) -> ProjectState:
    state = await db.get(ProjectState, project_id)
    if state is not None:
        return state
    state = ProjectState(
        project_id=project_id,
        autonomy_level=normalize_autonomy_level(autonomy_level),
        config=dict(config or {}),
    )
    try:
        async with db.begin_nested():
            db.add(state)
            await db.flush()
    except IntegrityError:
        existing = await db.get(ProjectState, project_id)
        if existing is None:
            raise
        return existing
    return state


async def transition_pm_phase(
    db: AsyncSession,
    item: WorkItem,
    to_phase: str,
    *,
    detail: str = "",
    payload: Mapping[str, Any] | None = None,
) -> WorkItemEvent:
    from_phase = item.pm_phase
    validate_transition(from_phase, to_phase)
    item.pm_phase = to_phase
    event_payload = dict(payload or {})
    event_payload.update({"from_phase": from_phase, "to_phase": to_phase})
    event = WorkItemEvent(
        work_item_id=item.id,
        kind="pm_phase",
        title=f"{from_phase} → {to_phase}",
        detail=detail,
        payload=event_payload,
    )
    db.add(event)
    await db.flush()
    logger.info(
        "pm.transition project=%s task=%s from=%s to=%s",
        item.project_id,
        item.id,
        from_phase,
        to_phase,
    )
    return event


transition_phase = transition_pm_phase


async def record_decision(
    db: AsyncSession,
    *,
    project_id: str,
    topic: str,
    decision: str,
    rationale: str = "",
    confirmed_by: str = "",
    source_message_id: str | None = None,
    context: Mapping[str, Any] | None = None,
    work_item_id: int | None = None,
    decision_key: str | None = None,
) -> DecisionRecord:
    key = decision_key or _stable_key(
        project_id,
        topic.strip(),
        work_item_id or "",
        decision.strip(),
        rationale.strip(),
        _canonical_json(context),
    )
    existing = await db.scalar(
        select(DecisionRecord).where(
            DecisionRecord.project_id == project_id,
            DecisionRecord.decision_key == key,
        )
    )
    if existing is not None:
        existing._pm_created = False
        return existing
    record = DecisionRecord(
        project_id=project_id,
        work_item_id=work_item_id,
        decision_key=key,
        topic=topic,
        decision=decision,
        rationale=rationale,
        confirmed_by=confirmed_by,
        source_message_id=source_message_id,
        context_json=dict(context or {}),
    )
    record._pm_created = True
    try:
        async with db.begin_nested():
            db.add(record)
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(
            select(DecisionRecord).where(
                DecisionRecord.project_id == project_id,
                DecisionRecord.decision_key == key,
            )
        )
        if existing is None:
            raise
        existing._pm_created = False
        return existing
    return record


def cursor_run_idempotency_key(
    work_item_id: int, attempt: int, request: Mapping[str, Any] | None = None
) -> str:
    return _stable_key(work_item_id, attempt, _canonical_json(request))


async def get_or_create_cursor_run(
    db: AsyncSession,
    item: WorkItem,
    *,
    attempt: int,
    request: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[CursorRun, bool]:
    locked_item = await db.scalar(
        select(WorkItem).where(WorkItem.id == item.id).with_for_update()
    )
    if locked_item is not None:
        item = locked_item
    if item.active_cursor_run_id:
        active = await db.get(CursorRun, item.active_cursor_run_id)
        if active is not None and active.status in {"pending", "running"}:
            requested_key = idempotency_key or cursor_run_idempotency_key(
                item.id, attempt, request
            )
            if active.idempotency_key == requested_key:
                return active, False
            raise ValueError(f"Work item {item.id} already has active Cursor run {active.id}")
    key = idempotency_key or cursor_run_idempotency_key(item.id, attempt, request)
    existing = await db.scalar(select(CursorRun).where(CursorRun.idempotency_key == key))
    if existing is not None:
        return existing, False
    existing_attempt = await db.scalar(
        select(CursorRun).where(
            CursorRun.work_item_id == item.id,
            CursorRun.attempt == attempt,
        )
    )
    if existing_attempt is not None:
        raise ValueError(
            f"Cursor attempt {attempt} already exists for work item {item.id} "
            "with a different idempotency key"
        )
    run = CursorRun(
        work_item_id=item.id,
        project_id=item.project_id or "",
        attempt=attempt,
        idempotency_key=key,
        request_json=dict(request or {}),
        status="pending",
    )
    try:
        async with db.begin_nested():
            db.add(run)
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(
            select(CursorRun).where(CursorRun.idempotency_key == key)
        )
        if existing is None:
            raise
        return existing, False
    item.active_cursor_run_id = run.id
    await db.flush()
    return run, True


async def create_cursor_run(
    db: AsyncSession,
    item: WorkItem,
    *,
    attempt: int,
    request: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> CursorRun:
    run, _ = await get_or_create_cursor_run(
        db,
        item,
        attempt=attempt,
        request=request,
        idempotency_key=idempotency_key,
    )
    return run


async def update_cursor_run(
    db: AsyncSession,
    run: CursorRun,
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> CursorRun:
    normalized = status.strip().lower()
    if normalized not in {"pending", "running", *CURSOR_RUN_TERMINAL_STATUSES}:
        raise ValueError(f"Unknown Cursor run status: {status}")
    run.status = normalized
    run.result_json = dict(result) if result is not None else None
    run.error = error
    if normalized == "running" and run.started_at is None:
        run.started_at = utcnow()
    if normalized in CURSOR_RUN_TERMINAL_STATUSES:
        run.completed_at = utcnow()
    await db.flush()
    logger.info(
        "pm.cursor_run project=%s task=%s run=%s attempt=%s status=%s",
        run.project_id,
        run.work_item_id,
        run.id,
        run.attempt,
        normalized,
    )
    return run


async def record_scope_change(
    db: AsyncSession,
    item: WorkItem,
    *,
    detail: str,
    source_message_id: str | None = None,
) -> WorkItemEvent:
    event = WorkItemEvent(
        work_item_id=item.id,
        kind="scope_change",
        title="Potential change request",
        detail=detail,
        payload={"source_message_id": source_message_id},
    )
    db.add(event)
    await db.flush()
    return event


persist_cursor_run = get_or_create_cursor_run
