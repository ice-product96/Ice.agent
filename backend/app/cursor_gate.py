"""Cursor completion rail: is the executor finished with THIS assignment?

Machine signals (done flag, agent status, live activity, pending approvals, prompt
landed, composer / task ids) are combined with the visible summary by the
``cursor_completion`` judge. Word lists such as "I'll", "проверю", "поиск" are gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import CursorRun, WorkItem
from .judgment import CompletionVerdict, JudgmentResult, JudgmentService


@dataclass
class CompletionDecision:
    state: str  # finished|working|needs_input|failed|foreign_result|legacy
    judged: bool
    reason: str = ""
    summary: str = ""
    judgment: JudgmentResult | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "judged": self.judged,
            "reason": self.reason,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


def _status_dict(value: Any) -> dict[str, Any]:
    from .cursorremote_drive import _as_status_dict

    return _as_status_dict(value) or {}


def completion_signals(result: dict[str, Any], *, item: WorkItem | None = None) -> dict[str, Any]:
    """Machine-readable facts about the executor, independent of any text."""
    last = _status_dict(result.get("last"))
    meta = item.metadata_json if item is not None and isinstance(item.metadata_json, dict) else {}
    structured = result.get("result") if isinstance(result.get("result"), dict) else {}
    impl = structured.get("implementation") if isinstance(structured.get("implementation"), dict) else {}
    files = impl.get("files_changed") if isinstance(impl.get("files_changed"), list) else result.get("files")
    return {
        "done_flag": bool(result.get("done")),
        "reported_status": str(result.get("status") or ""),
        "agent_status": str(last.get("agentStatus") or last.get("status") or ""),
        "agent_activity_live": bool(last.get("agentActivityLive")),
        "pending_approvals": int(last.get("pendingApprovalCount") or 0) or len(result.get("approvals") or []),
        "prompt_sent": bool(result.get("prompt_sent")),
        "prompt_visible": bool(result.get("prompt_visible")),
        "seen_busy": bool(result.get("seen_busy")),
        "started": bool(result.get("started")),
        "skipped_prompt": bool(result.get("skipped_prompt")),
        "callback": bool(result.get("callback")),
        "summary_equals_baseline": bool(
            str(result.get("baseline_summary") or "").strip()
            and str(result.get("summary") or "").strip() == str(result.get("baseline_summary") or "").strip()
        ),
        "files_changed": [str(v) for v in (files or [])][:60],
        "composer_id": str(result.get("cursor_composer_id") or ""),
        "expected_composer_id": str(meta.get("cursor_composer_id") or ""),
        "remote_task_id": str(result.get("cursor_remote_task_id") or ""),
        "expected_remote_task_id": str(meta.get("cursor_remote_task_id") or ""),
        "structured_task_id": str(structured.get("task_id") or ""),
        "structured_status": str(structured.get("status") or ""),
    }


def _summary_text(result: dict[str, Any]) -> str:
    structured = result.get("result") if isinstance(result.get("result"), dict) else {}
    impl = structured.get("implementation") if isinstance(structured.get("implementation"), dict) else {}
    for candidate in (result.get("summary"), impl.get("summary"), structured.get("summary")):
        text = str(candidate or "").strip()
        if text:
            return text[:8000]
    return ""


def completion_payload(item: WorkItem, run: CursorRun | None, result: dict[str, Any]) -> dict[str, Any]:
    request = run.request_json if run is not None and isinstance(run.request_json, dict) else {}
    brief = str(request.get("brief") or "")
    return {
        "assignment": {
            "work_item_id": item.id,
            "title": item.title,
            "goal": str(item.goal or "")[:1500],
            "acceptance_criteria": [str(v) for v in list(item.acceptance_criteria or [])][:20],
            "prompt_excerpt": brief[:1500],
            "attempt": run.attempt if run is not None else None,
        },
        "signals": completion_signals(result, item=item),
        "summary": _summary_text(result),
    }


def legacy_completion(result: dict[str, Any]) -> CompletionDecision:
    from .pm_state import is_leftover_cursor_idle

    if not result.get("done"):
        return CompletionDecision(state="working", judged=False, reason="done=false")
    if is_leftover_cursor_idle(result):
        return CompletionDecision(state="foreign_result", judged=False, reason="idle without our prompt")
    return CompletionDecision(state="finished", judged=False, reason="done=true", summary=_summary_text(result))


async def assess_completion(
    db: Any,
    item: WorkItem,
    run: CursorRun | None,
    result: dict[str, Any],
    *,
    judgment: JudgmentService | None,
    client: Any | None = None,
    project_config: dict[str, Any] | None = None,
) -> CompletionDecision:
    """Decide the executor state. Only consulted once the drive loop reports idle/done."""
    legacy = legacy_completion(result)
    service = judgment or JudgmentService(settings=None)
    mode = service.mode("cursor_completion", project_config)
    if mode == "off" or (not service.configured and client is None):
        return legacy
    signals = completion_signals(result, item=item)
    # Nothing to judge while the executor is visibly busy.
    if signals["agent_activity_live"] or (not signals["done_flag"] and signals["seen_busy"] and not signals["pending_approvals"]):
        return legacy
    if not signals["done_flag"] and not signals["pending_approvals"] and not _summary_text(result):
        return legacy
    result_j = await service.judge(
        "cursor_completion",
        completion_payload(item, run, result),
        db=db,
        agent_id=item.agent_id,
        work_item_id=item.id,
        chat_id=item.chat_id,
        project_id=item.project_id,
        project_config=project_config,
        legacy={"verdict": legacy.state},
        client=client,
    )
    verdict = result_j.verdict if isinstance(result_j.verdict, CompletionVerdict) else None
    if mode == "shadow" or verdict is None:
        legacy.judgment = result_j
        return legacy
    if not result_j.confident:
        # Unsure → keep waiting; a premature "finished" is the expensive mistake.
        return CompletionDecision(
            state="working" if legacy.state != "foreign_result" else "foreign_result",
            judged=True,
            reason=f"low confidence ({verdict.confidence:.2f}): {verdict.reasoning}",
            judgment=result_j,
        )
    return CompletionDecision(
        state=verdict.verdict,
        judged=True,
        reason=verdict.reasoning or verdict.verdict,
        summary=verdict.summary or _summary_text(result),
        judgment=result_j,
    )


def apply_completion_to_result(result: dict[str, Any], decision: CompletionDecision) -> dict[str, Any]:
    """Rewrite the drive result so downstream code sees the judged state."""
    if not decision.judged:
        return result
    adjusted = dict(result)
    adjusted["completion_judgment"] = decision.as_dict()
    if decision.state == "finished":
        adjusted["done"] = True
        adjusted["seen_busy"] = True
        adjusted["started"] = True
        adjusted.setdefault("status", "idle")
        if decision.summary and not str(adjusted.get("summary") or "").strip():
            adjusted["summary"] = decision.summary
    elif decision.state in {"working", "needs_input"}:
        adjusted["done"] = False
        adjusted["status"] = "needs_input" if decision.state == "needs_input" else "working"
        adjusted["seen_busy"] = True
        adjusted["started"] = True
        adjusted["prompt_sent"] = adjusted.get("prompt_sent", True)
        adjusted["needs_input"] = decision.state == "needs_input"
    elif decision.state == "foreign_result":
        adjusted["done"] = True
        adjusted["seen_busy"] = False
        adjusted["started"] = False
        adjusted["prompt_sent"] = False
        adjusted["skipped_prompt"] = True
        adjusted["foreign_result"] = True
    elif decision.state == "failed":
        adjusted["done"] = True
        adjusted["seen_busy"] = True
        adjusted["ok"] = False
        adjusted["status"] = "error"
        adjusted["result"] = {
            "task_id": "",
            "status": "failed",
            "implementation": {"summary": decision.summary or decision.reason or "Executor failed", "files_changed": [], "tests": []},
            "verification": {"tests_passed": False, "lint_passed": False, "acceptance_criteria": []},
            "questions": [],
            "risks": [decision.reason or "Executor failed"],
            "limitations": ["Judged as failed from executor signals"],
        }
    return adjusted
