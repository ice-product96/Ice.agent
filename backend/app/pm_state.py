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


def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    # #region agent log
    try:
        import time

        payload = {
            "sessionId": "2ac83f",
            "runId": "post-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        for path in (
            r"d:\projects\ice.agent\debug-2ac83f.log",
            "/app/data/debug-2ac83f.log",
            "/tmp/debug-2ac83f.log",
            "debug-2ac83f.log",
        ):
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except Exception:
                continue
    except Exception:
        pass
    # #endregion

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
        {"DEV_COMPLETE", "CHANGES_REQUESTED", "BLOCKED", "READY_FOR_DEV", "CANCELLED"}
    ),
    "BLOCKED": frozenset(
        {
            "CLARIFICATION",
            "CHANGES_REQUESTED",
            "READY_FOR_DEV",
            "IN_DEVELOPMENT",
            "DEV_COMPLETE",
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

SPEC_STATUSES = ("missing", "draft", "confirmed")
EXECUTION_VERDICTS = ("execute", "discuss", "draft_spec")
FEATURE_EXECUTE_MAX_MINUTES = 480
SPEC_SCOPE_FIELDS = ("in_scope", "out_of_scope", "modules", "goals")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def topic_is_spec_approval(topic: str, decision: str = "") -> bool:
    blob = f"{topic} {decision}".strip().casefold()
    tokens = {part for part in re.split(r"[\s/_,.;:]+", blob) if part}
    if tokens & {"tz", "spec", "тз"}:
        return True
    return any(marker in blob for marker in ("техническ", "спецификац", "тз проект"))


def normalize_project_spec(raw: Any) -> dict[str, Any]:
    data = dict(raw) if isinstance(raw, Mapping) else {}
    status = str(data.get("status") or "missing").strip().lower()
    if status not in SPEC_STATUSES:
        status = "missing"
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    return {
        "status": status,
        "summary": str(data.get("summary") or "").strip(),
        "goals": _string_list(data.get("goals")),
        "in_scope": _string_list(data.get("in_scope")),
        "out_of_scope": _string_list(data.get("out_of_scope")),
        "constraints": _string_list(data.get("constraints")),
        "modules": _string_list(data.get("modules")),
        "version": max(0, version),
        "updated_at": data.get("updated_at"),
        "confirmed_at": data.get("confirmed_at"),
        "confirmed_by": str(data.get("confirmed_by") or "").strip(),
    }


def spec_is_empty(spec: Mapping[str, Any] | None) -> bool:
    data = spec or {}
    return not any(
        [
            str(data.get("summary") or "").strip(),
            data.get("goals"),
            data.get("in_scope"),
            data.get("out_of_scope"),
            data.get("modules"),
        ]
    )


def read_project_spec(state: ProjectState | Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return normalize_project_spec({"status": "missing"})
    if isinstance(state, Mapping):
        config = dict(state.get("config") or {}) if "config" in state else dict(state)
        raw = config.get("spec") if "spec" in config else state.get("spec")
        if raw is None and not any(key in state for key in ("status", "in_scope", "summary")):
            return normalize_project_spec({"status": "missing"})
        return normalize_project_spec(raw if raw is not None else state)
    config = dict(state.config or {})
    raw = config.get("spec")
    if raw is None or raw == {}:
        return normalize_project_spec({"status": "missing"})
    return normalize_project_spec(raw)


def apply_spec_update(
    state: ProjectState,
    patch: Mapping[str, Any] | None,
    *,
    confirm: bool = False,
    confirmed_by: str = "",
    force_draft: bool = False,
) -> dict[str, Any]:
    current = read_project_spec(state)
    incoming = dict(patch or {})
    next_spec = dict(current)
    scope_changed = False
    changed = bool(confirm or force_draft)
    if incoming.get("summary") is not None:
        summary = str(incoming.get("summary") or "").strip()
        if summary != current.get("summary"):
            changed = True
        next_spec["summary"] = summary
    for key in ("goals", "in_scope", "out_of_scope", "constraints", "modules"):
        if key not in incoming or incoming[key] is None:
            continue
        new_list = _string_list(incoming[key])
        if new_list != list(current.get(key) or []):
            changed = True
            if key in SPEC_SCOPE_FIELDS:
                scope_changed = True
        next_spec[key] = new_list
    if not changed:
        return current
    now = utcnow().isoformat()
    next_spec["updated_at"] = now
    next_spec["version"] = int(current.get("version") or 0) + 1
    if confirm:
        next_spec["status"] = "confirmed"
        next_spec["confirmed_at"] = now
        next_spec["confirmed_by"] = str(confirmed_by or "").strip()
    elif force_draft or (scope_changed and current.get("status") == "confirmed"):
        next_spec["status"] = "draft"
        next_spec["confirmed_at"] = None
        next_spec["confirmed_by"] = ""
    elif spec_is_empty(next_spec):
        next_spec["status"] = "missing"
        next_spec["confirmed_at"] = None
        next_spec["confirmed_by"] = ""
    elif current.get("status") == "missing":
        next_spec["status"] = "draft"
    else:
        next_spec["status"] = current.get("status") or "draft"
    config = dict(state.config or {})
    config["spec"] = next_spec
    state.config = config
    return next_spec


def confirm_project_spec(
    state: ProjectState,
    *,
    confirmed_by: str = "",
) -> dict[str, Any]:
    return apply_spec_update(state, {}, confirm=True, confirmed_by=confirmed_by)


def revert_spec_to_draft(state: ProjectState) -> dict[str, Any]:
    current = read_project_spec(state)
    if current.get("status") != "confirmed":
        return current
    return apply_spec_update(state, {}, force_draft=True)


_INTAKE_HEAD_RE = re.compile(
    r"^сводка задания заказчика[^\n]*\n?",
    re.IGNORECASE,
)
_ISO_TAIL_RE = re.compile(r"\(\d{4}-\d{2}-\d{2}T[^)]+\)\s*$")


def spec_summary_from_item(item: WorkItem) -> str:
    text = str(item.goal or item.title or "").strip()
    text = _INTAKE_HEAD_RE.sub("", text).strip()
    pieces: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^\d+\.\s*", "", raw).strip()
        line = _ISO_TAIL_RE.sub("", line).strip()
        if line:
            pieces.append(line)
    summary = " ".join(pieces).strip() or str(item.title or "").strip()
    return summary[:500]


def seed_draft_spec_from_item(state: ProjectState, item: WorkItem) -> dict[str, Any]:
    current = read_project_spec(state)
    if current.get("status") != "missing" and not spec_is_empty(current):
        return current
    summary = spec_summary_from_item(item)
    goals = list(item.requirements or [])[:8]
    if not goals and summary:
        goals = [summary]
    return apply_spec_update(
        state,
        {
            "summary": summary,
            "goals": goals,
            "constraints": list(item.constraints or []),
        },
    )


def assess_execution(
    item: WorkItem,
    spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec_norm = normalize_project_spec(spec)
    reasons: list[str] = []
    questions: list[str] = []
    verdict = "execute"
    status = str(spec_norm.get("status") or "missing")

    def demote(next_verdict: str) -> None:
        nonlocal verdict
        rank = {"execute": 0, "discuss": 1, "draft_spec": 2}
        if rank.get(next_verdict, 0) > rank.get(verdict, 0):
            verdict = next_verdict

    if spec_is_empty(spec_norm) or status == "missing":
        demote("draft_spec")
        reasons.append("Project spec is missing")
        questions.append(
            "Какие цели продукта, что входит в in_scope и что явно вне scope?"
        )
    elif status != "confirmed":
        demote("discuss")
        reasons.append("Project spec is not confirmed")
        questions.append(
            "Подтвердите ТЗ проекта (цели, in_scope, out_of_scope), "
            "затем pm_record_decision с темой тз/spec/tz."
        )

    issues = readiness_issues(item)
    if issues:
        demote("discuss")
        reasons.extend(issues)
        questions.append("Уточните цель, требования и проверяемые критерии приёмки.")

    minutes = estimated_duration_minutes(item)
    if (
        str(item.task_type or "").strip().lower() == "feature"
        and minutes is not None
        and minutes > FEATURE_EXECUTE_MAX_MINUTES
    ):
        demote("discuss")
        reasons.append("Feature estimate exceeds one working day")
        questions.append(
            "Можно ли сузить срез до одного рабочего дня, или это несколько этапов?"
        )

    result = {
        "verdict": verdict,
        "reasons": _uniq(reasons),
        "questions": _uniq(questions),
        "spec_version": int(spec_norm.get("version") or 0),
        "spec_status": status,
    }
    # #region agent log
    _agent_dbg(
        "A",
        "pm_state.py:assess_execution",
        "execution verdict (no lexical scope)",
        {
            "item_id": getattr(item, "id", None),
            "phase": getattr(item, "pm_phase", None),
            "task_type": getattr(item, "task_type", None),
            "lexical_scope": False,
            **result,
        },
    )
    # #endregion
    return result


def stamp_execution_verdict(
    item: WorkItem,
    verdict: Mapping[str, Any],
) -> dict[str, Any]:
    ctx = dict(item.context_json or {}) if isinstance(item.context_json, dict) else {}
    payload = {
        "verdict": str(verdict.get("verdict") or "discuss"),
        "reasons": list(verdict.get("reasons") or []),
        "questions": list(verdict.get("questions") or []),
        "spec_version": int(verdict.get("spec_version") or 0),
        "spec_status": str(verdict.get("spec_status") or "missing"),
    }
    if verdict.get("drafted"):
        payload["drafted"] = True
    ctx["execution"] = payload
    item.context_json = ctx
    return payload


def apply_execution_assessment(
    item: WorkItem,
    spec: Mapping[str, Any] | None = None,
    *,
    project_state: ProjectState | None = None,
    draft_if_missing: bool = True,
) -> dict[str, Any]:
    spec_norm = normalize_project_spec(
        spec if spec is not None else read_project_spec(project_state)
    )
    verdict = assess_execution(item, spec_norm)
    if (
        draft_if_missing
        and verdict.get("verdict") == "draft_spec"
        and project_state is not None
        and (spec_norm.get("status") == "missing" or spec_is_empty(spec_norm))
    ):
        spec_norm = seed_draft_spec_from_item(project_state, item)
        verdict = assess_execution(item, spec_norm)
        if verdict.get("verdict") == "discuss" and spec_norm.get("status") == "draft":
            verdict["verdict"] = "draft_spec"
        verdict["drafted"] = True
        verdict["spec_version"] = int(spec_norm.get("version") or 0)
        verdict["spec_status"] = str(spec_norm.get("status") or "draft")
    return stamp_execution_verdict(item, verdict)


def execution_verdict_of(item: WorkItem | None) -> str:
    if item is None:
        return ""
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    execution = ctx.get("execution") if isinstance(ctx.get("execution"), dict) else {}
    return str(execution.get("verdict") or "").strip().lower()


def execution_gate_error(verdict: Mapping[str, Any]) -> str:
    reasons = "; ".join(str(item) for item in list(verdict.get("reasons") or []) if item)
    questions = " ".join(str(item) for item in list(verdict.get("questions") or []) if item)
    parts = [
        "Task is not ready for Cursor.",
        reasons or f"verdict={verdict.get('verdict') or 'discuss'}",
    ]
    if questions:
        parts.append(f"Ask the customer: {questions}")
    parts.append("Do not call submit_development_task until verdict is execute.")
    return " ".join(parts)


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


MANAGER_CONFIRMERS = ("manager", "owner", "admin", "руководитель", "владелец")


def is_client_confirmer(
    confirmed_by: str,
    *,
    source_message_id: str | None = None,
) -> bool:
    """True when a stored decision came from the customer, not the human manager."""
    value = (confirmed_by or "").strip().lower()
    if any(marker in value for marker in MANAGER_CONFIRMERS):
        return False
    if value:
        return True
    return bool(str(source_message_id or "").strip())


def development_is_client_confirmed(
    item: WorkItem,
    *,
    has_client_decision: bool = False,
) -> bool:
    if item.pm_phase in {"CLIENT_CONFIRMED", "READY_FOR_DEV", "CHANGES_REQUESTED"}:
        return True
    return has_client_decision


async def item_has_client_confirmation(db: AsyncSession, item: WorkItem) -> bool:
    if development_is_client_confirmed(item, has_client_decision=False):
        return True
    rows = list(
        await db.scalars(
            select(DecisionRecord).where(DecisionRecord.work_item_id == item.id)
        )
    )
    return any(
        is_client_confirmer(row.confirmed_by, source_message_id=row.source_message_id)
        for row in rows
    )


SMALL_FIX_MAX_MINUTES = 120


def work_item_is_tracker_sourced(item: WorkItem | None) -> bool:
    """A card already on the customer ice_tracker board is the request."""
    if item is None:
        return False
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    meta = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    pm = meta.get("pm") if isinstance(meta.get("pm"), dict) else {}
    for source in (ctx, pm):
        if not isinstance(source, dict):
            continue
        if str(source.get("tracker_task_id") or source.get("card_id") or "").strip():
            return True
    return False


def estimated_duration_minutes(item: WorkItem | None) -> float | None:
    if item is None:
        return None
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    for key in ("estimated_duration_minutes", "min_execution_minutes"):
        raw = ctx.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def infer_inside_agreed_scope(
    item: WorkItem | None,
    *,
    client_confirmed: bool = False,
) -> bool:
    if item is None:
        return bool(client_confirmed)
    if execution_verdict_of(item) == "execute":
        return True
    if work_item_is_tracker_sourced(item):
        return False
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    if "inside_agreed_scope" in ctx:
        return bool(ctx.get("inside_agreed_scope"))
    return bool(client_confirmed)


def infer_small_fix(item: WorkItem | None) -> bool:
    if item is None:
        return False
    ctx = item.context_json if isinstance(item.context_json, dict) else {}
    if ctx.get("high_risk"):
        return False
    if ctx.get("small_fix"):
        return True
    minutes = estimated_duration_minutes(item)
    if minutes is not None and minutes <= SMALL_FIX_MAX_MINUTES:
        return True
    return work_item_is_tracker_sourced(item) and str(
        item.task_type or ""
    ).strip().lower() == "bug"


def stamp_autonomy_flags(
    item: WorkItem,
    *,
    client_confirmed: bool = False,
) -> WorkItem:
    """Fill scope/small_fix so LEVEL_1 tracker bugs are not blocked on LLM flags."""
    ctx = dict(item.context_json or {}) if isinstance(item.context_json, dict) else {}
    ctx["inside_agreed_scope"] = infer_inside_agreed_scope(
        item, client_confirmed=client_confirmed
    )
    ctx["small_fix"] = infer_small_fix(item)
    item.context_json = ctx
    return item


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
    previous_pm = metadata.get("pm") if isinstance(metadata.get("pm"), dict) else {}
    metadata["pm"] = {
        "dependencies": contract.dependencies,
        "related_tasks": contract.related_tasks,
        "source": contract.source,
    }
    for key in ("tracker_task_id", "tracker_project_id", "card_id"):
        value = str(
            contract.context.get(key) or previous_pm.get(key) or ""
        ).strip()
        if value:
            metadata["pm"][key] = value
    item.metadata_json = metadata
    return item


def _lines(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


_CURSOR_BRIEF_DROP_KEYS = {
    "tracker",
    "tracker_project_id",
    "tracker_task_ids",
    "board_id",
    "card_id",
    "card_ids",
    "section_id",
    "ice_tracker",
    "related_tracker_tasks",
    "estimated_cost",
    "estimated_duration_minutes",
    "min_execution_minutes",
    "min_execution_ratio",
    "hourly_rate",
    "currency",
    "cost_approved",
    "cost_decision_id",
    "cost_requires_customer_approval",
    "ask_customer_about_cost",
    "elapsed_cursor_minutes",
    "min_execution_remaining_minutes",
    "wait_estimated_duration",
    "execution",
    "inside_agreed_scope",
    "small_fix",
    "high_risk",
    "owner_approved",
}
_CURSOR_BRIEF_DROP_MARKERS = (
    "cost",
    "price",
    "оплат",
    "стоим",
    "hourly",
    "tracker_",
)


def _cursor_brief_drops_context_key(key: Any) -> bool:
    name = str(key or "").strip()
    if not name:
        return True
    lowered = name.casefold()
    if lowered in _CURSOR_BRIEF_DROP_KEYS:
        return True
    return any(marker in lowered for marker in _CURSOR_BRIEF_DROP_MARKERS)


def render_task_brief(item: WorkItem) -> str:
    title = str(item.title or "").strip() or f"Work item {item.id}"
    task_type = str(item.task_type or "task")
    priority = str(item.priority or "normal")
    project_label = str(item.project_id or "").strip() or "unspecified"
    # Tracker board UUIDs belong in ice_tracker, not in the Cursor brief.
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        project_label,
        flags=re.IGNORECASE,
    ):
        project_label = "unspecified"
    sections = [
        f"# Task: {title}",
        f"**Type:** {task_type}",
        f"**Priority:** {priority}",
        f"**Project:** {project_label}",
        f"**task_id:** {item.id}",
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
    context = dict(item.context_json or {}) if isinstance(item.context_json, dict) else {}
    clean_context = {
        key: value
        for key, value in context.items()
        if not _cursor_brief_drops_context_key(key)
    }
    if clean_context:
        payload = json.dumps(clean_context, ensure_ascii=False, sort_keys=True, indent=2)
        sections.append(f"## Context\n```json\n{payload}\n```")
    sections.append(
        "Work only on this engineering brief. When finished, write a short summary of "
        "what you implemented and how to verify it. Do not discuss price, cost, or schedule. "
        "Do not update trackers, boards, or cards — the project manager handles ice_tracker separately."
    )
    return "\n\n".join(sections).strip() + "\n"


task_brief = render_task_brief


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object from prose or a fenced block."""
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def is_leftover_cursor_idle(result: Mapping[str, Any] | None) -> bool:
    """True when Cursor looks idle but this assignment did not actually run."""
    if not isinstance(result, Mapping):
        return False
    if result.get("skipped_prompt") and not (
        result.get("started") or result.get("seen_busy")
    ):
        # skipped_prompt alone means "did not send"; if Composer was busy for us, keep it.
        return True
    if result.get("done") and not result.get("seen_busy"):
        # Instant idle after send_task is leftover plan/UI, not a finished job.
        return True
    if result.get("prompt_sent") and result.get("seen_busy"):
        return False
    if result.get("seen_busy"):
        return False
    # check_and_drive / busy-skip path: idle Composer from another chat.
    return bool(result.get("done"))


def _norm_criterion(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _evidence_row_passed(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    return row.get("passed") is True and bool(str(row.get("evidence") or "").strip())


def _cursor_result_summary_text(run: CursorRun | None) -> str:
    if run is None:
        return ""
    payload = run.result_json if isinstance(run.result_json, dict) else {}
    impl = payload.get("implementation") if isinstance(payload.get("implementation"), dict) else {}
    parts = [
        str(impl.get("summary") or "").strip(),
        str(payload.get("summary") or "").strip(),
        str(payload.get("customer_response") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


_DONE_SUMMARY_MARKERS = (
    "уже реализ",
    "уже есть",
    "уже в проект",
    "покрывает все",
    "дополнительных изменений не",
    "не потребовалось",
    "implemented",
    "already implemented",
    "covers all",
    "no additional",
    "fully covered",
)


def summary_covers_acceptance(item: WorkItem, run: CursorRun | None) -> bool:
    """DEPRECATED keyword heuristic. Kept only for shadow comparison in evals.

    No longer consulted by ``cursor_run_satisfies_acceptance``: prose that mentions the
    right words is not evidence. The ``qa_verifier`` judge decides from meaning.
    """
    if run is None or run.status != "completed":
        return False
    criteria = [
        str(value).strip()
        for value in list(item.acceptance_criteria or [])
        if str(value or "").strip()
    ]
    if not criteria:
        return False
    text = _norm_criterion(_cursor_result_summary_text(run))
    if len(text) < 80:
        return False
    if not any(marker in text for marker in _DONE_SUMMARY_MARKERS):
        return False
    for criterion in criteria:
        tokens = [
            token
            for token in _norm_criterion(criterion).split()
            if len(token) > 3
        ]
        if not tokens:
            continue
        hits = sum(1 for token in tokens if token in text)
        if hits < max(1, (len(tokens) + 1) // 2):
            return False
    return True


def match_acceptance_evidence(
    criterion: str,
    rows: list[Any] | None,
) -> dict[str, Any] | None:
    """Match one stored criterion to Cursor evidence, allowing whitespace/case drift only."""
    want = _norm_criterion(criterion)
    if not want:
        return None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if _norm_criterion(row.get("criterion")) == want:
            return row
    return None


def cursor_run_satisfies_acceptance(item: WorkItem, run: CursorRun | None) -> bool:
    """Machine-signal path: structured verification rows pass for every criterion.

    This is a legacy signal used for shadow comparison and as the fallback when no QA
    judge is configured. Recovered/truncated or prose-only results never satisfy it.
    """
    if run is None or run.status != "completed":
        return False
    payload = run.result_json if isinstance(run.result_json, dict) else {}
    if payload.get("truncated_recovery") or payload.get("native_summary"):
        return False
    verification = payload.get("verification")
    if not isinstance(verification, dict):
        return False
    if (
        verification.get("tests_passed") is not True
        or verification.get("lint_passed") is not True
    ):
        return False
    criteria = [
        str(value).strip()
        for value in list(item.acceptance_criteria or [])
        if str(value or "").strip()
    ]
    if not criteria:
        return False
    rows = verification.get("acceptance_criteria")
    if not isinstance(rows, list):
        return False
    return all(
        _evidence_row_passed(match_acceptance_evidence(criterion, rows))
        for criterion in criteria
    )


async def latest_completed_cursor_run(
    db: AsyncSession,
    item: WorkItem,
) -> CursorRun | None:
    from .cursorremote_drive import summary_looks_incomplete

    runs = list(
        await db.scalars(
            select(CursorRun)
            .where(
                CursorRun.work_item_id == item.id,
                CursorRun.status == "completed",
            )
            .order_by(CursorRun.attempt.desc(), CursorRun.id.desc())
        )
    )
    for run in runs:
        data = run.result_json if isinstance(run.result_json, dict) else {}
        impl = data.get("implementation") if isinstance(data.get("implementation"), dict) else {}
        summary = str(impl.get("summary") or data.get("summary") or "")
        if data.get("native_summary") and summary_looks_incomplete(summary):
            continue
        return run
    return None


def recover_truncated_cursor_result(
    text: str,
    *,
    expected_task_id: str,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any] | None:
    """Rebuild a structured result when CursorRemote truncates the assistant JSON (~2k)."""
    raw = (text or "").strip()
    if not raw:
        return None
    tid_match = re.search(r'"task_id"\s*:\s*"?(\d+)"?', raw)
    if tid_match is None or tid_match.group(1) != str(expected_task_id).strip():
        return None
    status_match = re.search(r'"status"\s*:\s*"([^"]+)"', raw)
    status = (status_match.group(1) if status_match else "").strip().lower()
    status = {"success": "completed", "succeeded": "completed", "error": "failed"}.get(
        status, status
    )
    if status not in CURSOR_RUN_TERMINAL_STATUSES:
        return None
    impl_summary = ""
    impl_match = re.search(
        r'"implementation"\s*:\s*\{[^{}]*?"summary"\s*:\s*"((?:\\.|[^"\\])*)"',
        raw,
        flags=re.DOTALL,
    )
    if impl_match:
        try:
            impl_summary = json.loads(f'"{impl_match.group(1)}"')
        except json.JSONDecodeError:
            impl_summary = impl_match.group(1)
    if not impl_summary:
        impl_summary = (
            "Cursor returned a truncated structured completion for this task "
            "(payload clipped by CursorRemote)."
        )
    criteria_payload: list[dict[str, Any]] = []
    for criterion in acceptance_criteria or []:
        text_c = str(criterion or "").strip()
        if not text_c:
            continue
        # Truncated JSON is not evidence: every criterion stays unverified for QA.
        criteria_payload.append(
            {
                "criterion": text_c,
                "passed": False,
                "evidence": "Recovered from truncated Cursor JSON; not verified.",
            }
        )
    return {
        "task_id": str(expected_task_id),
        "status": status,
        "implementation": {
            "summary": impl_summary,
            "files_changed": [],
            "tests": [],
        },
        "verification": {
            "tests_passed": False,
            "lint_passed": False,
            "acceptance_criteria": criteria_payload,
        },
        "questions": [],
        "risks": [
            "CursorRemote truncated the assistant JSON; evidence may need manual QA."
        ],
        "limitations": ["Recovered from truncated Cursor payload"],
        "truncated_recovery": True,
    }


def native_cursor_summary_as_result(
    summary: str,
    *,
    expected_task_id: str,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any] | None:
    """Treat Cursor's post-run prose summary as a completion for QA."""
    text = str(summary or "").strip()
    if len(text) < 24:
        return None
    from .cursorremote_drive import summary_looks_incomplete

    if summary_looks_incomplete(text):
        return None
    tid_match = re.search(r'"task_id"\s*:\s*"?(\d+)"?', text)
    if tid_match is not None and tid_match.group(1) != str(expected_task_id).strip():
        return None
    criteria_payload: list[dict[str, Any]] = []
    for criterion in acceptance_criteria or []:
        text_c = str(criterion or "").strip()
        if not text_c:
            continue
        criteria_payload.append(
            {
                "criterion": text_c,
                "passed": False,
                "evidence": "Cursor posted a completion summary; verify this criterion in QA.",
            }
        )
    return {
        "task_id": str(expected_task_id),
        "status": "completed",
        "implementation": {
            "summary": text[:8000],
            "files_changed": [],
            "tests": [],
        },
        "verification": {
            "tests_passed": False,
            "lint_passed": False,
            "acceptance_criteria": criteria_payload,
        },
        "questions": [],
        "risks": ["Native Cursor summary — PM must verify acceptance criteria."],
        "limitations": ["No structured JSON payload; used Cursor completion summary."],
        "native_summary": True,
    }


def parse_cursor_result(result: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, Mapping):
        parsed = dict(result)
    else:
        if not isinstance(result, str):
            raise ValueError("Cursor result must be a JSON object or JSON string")
        value = extract_json_object(result)
        if value is None:
            raise ValueError("Cursor result must be a structured JSON object")
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
    mcp: Any = None,
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
    tracker_result: dict[str, Any] | None = None
    try:
        from .tracker_poll import sync_work_item_tracker_card

        tracker_result = await sync_work_item_tracker_card(
            item, phase=to_phase, mcp=mcp, db=db
        )
    except Exception as exc:
        logger.warning(
            "tracker.sync after %s→%s work_item=%s failed: %s",
            from_phase,
            to_phase,
            item.id,
            exc,
        )
        tracker_result = {"ok": False, "error": str(exc)[:500]}
    if tracker_result and (
        tracker_result.get("moved")
        or tracker_result.get("completed")
        or tracker_result.get("updated_status")
        or tracker_result.get("error")
    ):
        event_payload = dict(event.payload or {})
        event_payload["tracker"] = tracker_result
        event.payload = event_payload
        if tracker_result.get("error") and to_phase in {"DONE", "CANCELLED"}:
            item.last_error = f"Tracker: {tracker_result.get('error')}"[:2000]
        db.add(
            WorkItemEvent(
                work_item_id=item.id,
                kind="tracker",
                title=(
                    "Карточка трекера закрыта"
                    if tracker_result.get("completed")
                    else f"Карточка трекера → {tracker_result.get('to_section') or tracker_result.get('lane')}"
                ),
                detail=str(
                    tracker_result.get("error")
                    or tracker_result.get("to_section")
                    or tracker_result.get("status")
                    or ""
                )[:800],
                payload=tracker_result,
            )
        )
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
