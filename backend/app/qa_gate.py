"""QA rail: the only code allowed to say a case is DONE.

The verdict comes from the ``qa_verifier`` judge (criterion by criterion, with
verbatim evidence). Machine signals from a structured Cursor result remain a
legacy path for shadow comparison; prose summaries never count as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .db import CursorRun, WorkItem
from .judgment import JudgmentResult, JudgmentService, QaVerdict

DEFAULT_EVIDENCE_FIX_REQUEST = (
    "QA could not verify the acceptance criteria from your report. For EVERY acceptance "
    "criterion listed in the brief, state whether it is implemented, how you verified it "
    "(test name, command output, manual check) and quote the concrete evidence. Return the "
    "structured JSON completion with `verification.acceptance_criteria` filled per criterion."
)

_TEXT_CLIP = 6000


@dataclass
class QaDecision:
    accept: bool
    verdict: str
    reason: str
    fix_request: str = ""
    customer_summary: str = ""
    should_request_fix: bool = False
    legacy: bool = False
    judgment: JudgmentResult | None = None
    criteria: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accept": self.accept,
            "verdict": self.verdict,
            "reason": self.reason,
            "fix_request": self.fix_request,
            "should_request_fix": self.should_request_fix,
            "legacy": self.legacy,
            "criteria": self.criteria,
            "judgment": self.judgment.as_dict() if self.judgment is not None else None,
        }


def _clip(value: Any, limit: int = _TEXT_CLIP) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _criteria(item: WorkItem) -> list[str]:
    return [str(value).strip() for value in list(item.acceptance_criteria or []) if str(value or "").strip()]


def qa_payload(item: WorkItem, run: CursorRun) -> dict[str, Any]:
    """Everything the QA judge may look at. Nothing else influences the verdict."""
    data = run.result_json if isinstance(run.result_json, dict) else {}
    impl = data.get("implementation") if isinstance(data.get("implementation"), dict) else {}
    verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
    rows = verification.get("acceptance_criteria")
    return {
        "task": {
            "id": item.id,
            "title": item.title,
            "goal": _clip(item.goal, 2000),
            "requirements": [str(v) for v in list(item.requirements or [])][:40],
            "acceptance_criteria": _criteria(item),
            "constraints": [str(v) for v in list(item.constraints or [])][:20],
            "edge_cases": [str(v) for v in list(item.edge_cases or [])][:20],
        },
        "executor_report": {
            "run_id": run.id,
            "attempt": run.attempt,
            "status": run.status,
            "summary": _clip(impl.get("summary") or data.get("summary") or ""),
            "customer_response": _clip(data.get("customer_response") or "", 2000),
            "files_changed": [str(v) for v in (impl.get("files_changed") or [])][:80],
            "tests": [str(v) for v in (impl.get("tests") or [])][:40],
            "verification": {
                "tests_passed": verification.get("tests_passed"),
                "lint_passed": verification.get("lint_passed"),
                "acceptance_criteria": rows if isinstance(rows, list) else [],
            },
            "questions": [str(v) for v in (data.get("questions") or [])][:20],
            "risks": [str(v) for v in (data.get("risks") or [])][:20],
            "limitations": [str(v) for v in (data.get("limitations") or [])][:20],
            "native_summary_only": bool(data.get("native_summary")),
            "recovered_from_truncated_json": bool(data.get("truncated_recovery")),
        },
    }


def _legacy_decision(item: WorkItem, run: CursorRun | None) -> bool:
    from .pm_state import cursor_run_satisfies_acceptance

    return cursor_run_satisfies_acceptance(item, run)


def _verdict_criteria(verdict: QaVerdict) -> list[dict[str, Any]]:
    return [
        {
            "criterion": row.criterion,
            "verdict": row.verdict,
            "evidence": [quote.model_dump() for quote in row.evidence],
            "note": row.note,
        }
        for row in verdict.criteria
    ]


async def evaluate_qa(
    db: Any,
    item: WorkItem,
    run: CursorRun | None,
    *,
    judgment: JudgmentService | None,
    client: Any | None = None,
    project_config: dict[str, Any] | None = None,
) -> QaDecision:
    """Decide whether the case may be accepted. Never raises."""
    if run is None or run.status != "completed":
        return QaDecision(accept=False, verdict="no_run", reason="No completed Cursor run to verify")
    if not _criteria(item):
        return QaDecision(
            accept=False,
            verdict="no_criteria",
            reason="Task has no acceptance criteria; nothing can be verified",
        )
    legacy = _legacy_decision(item, run)
    service = judgment
    if service is None:
        service = JudgmentService(settings=None)
    mode = service.mode("qa_verifier", project_config)
    legacy_payload = {"verdict": "accept" if legacy else "fix_required"}
    if mode == "off" or (not service.configured and client is None):
        # Legacy machine-signal path only (shadow without any judge model is the same).
        return QaDecision(
            accept=legacy,
            verdict="legacy_accept" if legacy else "legacy_reject",
            reason=(
                "Structured verification rows pass for every criterion"
                if legacy
                else "Structured verification lacks passing evidence for every criterion"
            ),
            should_request_fix=not legacy,
            fix_request="" if legacy else DEFAULT_EVIDENCE_FIX_REQUEST,
            legacy=True,
        )

    result = await service.judge(
        "qa_verifier",
        qa_payload(item, run),
        db=db,
        agent_id=item.agent_id,
        work_item_id=item.id,
        chat_id=item.chat_id,
        project_id=item.project_id,
        project_config=project_config,
        legacy=legacy_payload,
        client=client,
    )
    verdict = result.verdict if isinstance(result.verdict, QaVerdict) else None
    criteria = _verdict_criteria(verdict) if verdict is not None else []

    if mode == "shadow":
        fix_hint = (verdict.fix_request if verdict is not None and verdict.verdict != "accept" else "") or ""
        return QaDecision(
            accept=legacy,
            verdict="legacy_accept" if legacy else "legacy_reject",
            reason=(
                "Structured verification rows pass for every criterion (judge in shadow)"
                if legacy
                else "Structured verification lacks passing evidence (judge in shadow)"
            ),
            should_request_fix=not legacy,
            fix_request="" if legacy else (fix_hint or DEFAULT_EVIDENCE_FIX_REQUEST),
            customer_summary=verdict.customer_summary if verdict is not None else "",
            legacy=True,
            judgment=result,
            criteria=criteria,
        )

    # enforce: the judge is authoritative; rails fail closed on degradation.
    if verdict is None:
        return QaDecision(
            accept=False,
            verdict="degraded",
            reason=f"QA judge unavailable: {result.error or 'no verdict'}",
            judgment=result,
        )
    if verdict.verdict == "accept":
        if result.confident:
            return QaDecision(
                accept=True,
                verdict="accept",
                reason=verdict.reasoning or "Every acceptance criterion verified with evidence",
                customer_summary=verdict.customer_summary,
                judgment=result,
                criteria=criteria,
            )
        return QaDecision(
            accept=False,
            verdict="low_confidence",
            reason=(
                f"QA judge accepted with confidence {verdict.confidence:.2f} below threshold "
                f"{result.threshold:.2f}; missing: {', '.join(verdict.missing) or 'n/a'}"
            ),
            judgment=result,
            criteria=criteria,
        )
    if verdict.verdict == "fix_required":
        if not result.confident:
            return QaDecision(
                accept=False,
                verdict="low_confidence",
                reason=(
                    f"QA judge requested a fix with confidence {verdict.confidence:.2f} below "
                    f"threshold {result.threshold:.2f}"
                ),
                fix_request=verdict.fix_request,
                judgment=result,
                criteria=criteria,
            )
        failed = [row.criterion for row in verdict.criteria if row.verdict != "pass"]
        return QaDecision(
            accept=False,
            verdict="fix_required",
            reason=verdict.reasoning or f"Criteria not met: {', '.join(failed[:6])}",
            fix_request=verdict.fix_request or _fix_request_from_criteria(verdict),
            should_request_fix=True,
            judgment=result,
            criteria=criteria,
        )
    # insufficient_evidence → ask the executor for verification, never close.
    return QaDecision(
        accept=False,
        verdict="insufficient_evidence",
        reason=verdict.reasoning or "Executor report is too thin to verify the criteria",
        fix_request=verdict.fix_request or DEFAULT_EVIDENCE_FIX_REQUEST,
        should_request_fix=True,
        judgment=result,
        criteria=criteria,
    )


def _fix_request_from_criteria(verdict: QaVerdict) -> str:
    lines = ["The following acceptance criteria are not verified. Implement and prove each one:"]
    for row in verdict.criteria:
        if row.verdict == "pass":
            continue
        note = f" — {row.note}" if row.note else ""
        lines.append(f"- [{row.verdict}] {row.criterion}{note}")
    return "\n".join(lines)
