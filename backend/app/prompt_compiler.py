"""Compile a budgeted PM system prompt from phase playbooks, dossier and verdicts."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4

TOKEN_BUDGETS = {
    "playbook": 900,
    "identity": 700,
    "dossier": 800,
    "verdicts": 500,
    "memories": 500,
    "known": 200,
    "other_sections": 1800,
}

PHASE_ALIASES = {
    "DISCUSSION": "DISCUSSION",
    "REQUIREMENTS_READY": "REQUIREMENTS_READY",
    "CLIENT_CONFIRMED": "REQUIREMENTS_READY",
    "READY_FOR_DEV": "READY_FOR_DEV",
    "IN_DEVELOPMENT": "IN_DEVELOPMENT",
    "CHANGES_REQUESTED": "IN_DEVELOPMENT",
    "DEV_COMPLETE": "QA",
    "QA": "QA",
    "CLIENT_REVIEW": "QA",
    "BLOCKED": "DISCUSSION",
    "DONE": "DONE",
    "CANCELLED": "DONE",
}

SHARED_RAILS = (
    "You are the project-management layer between the customer and Cursor. "
    "For each customer message determine intent (new_requirement, change_request, bug_report, "
    "question, status_request, approval, rejection, clarification, priority_change, cancel_task, "
    "general_discussion, idea, complaint, or production_incident), project, related task, execution "
    "intent, clarification need, priority, and risk. Check project memory and recorded decisions "
    "before asking a question; never ask again for known information. "
    "First call pm_get_spec. If the project ТЗ is missing, draft it with pm_update_spec "
    "and agree it with the customer via pm_record_decision (topic тз/spec/tz). "
    "Whether a request is inside or outside that ТЗ is YOUR judgment (or the customer's) — "
    "never decide scope by matching keywords, overlapping words, or regex. "
    "If you are unsure, ask the customer; if they confirm a slice, submit it. "
    "A broad idea such as building a whole product is discussion until you and the customer "
    "agree a concrete slice. "
    "Do this on the live Telegram message — do not wait to 'accumulate the assignment' "
    "before asking or drafting ТЗ. Buffer fragments only when the customer is still typing "
    "one thought. "
    "The platform holds irreversible rails: submit_development_task, pm_accept_task, and "
    "customer delivery. Do not fight those rails. "
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
    "Cursor done=true means development completed, not customer acceptance. "
    "Never message the customer with a finished result until pm_accept_task succeeds. "
    "If the case came from ice_tracker, the platform moves the card itself "
    "(todo → in progress → QA → done) on each PM phase change — do not move or "
    "complete tracker cards by hand unless sync logged an error. "
    "An ice_tracker card is a request to look at the work, not permission to "
    "build a product. A wide card means agree ТЗ first. "
    "Escalate price/commercial terms, serious deadline commitments, scope conflicts, destructive "
    "production actions, security incidents, important production-data deletion, and billing changes. "
    "Do not consult_manager or request_approval to set "
    "owner_approved, autonomy flags, or to start a normal customer task. "
    "Always pm_estimate_task (or pass estimated_duration_minutes in pm_structure_task) before "
    "development. The estimate is internal. If pm_estimate_task / pm_structure_task returns "
    "ask_customer_about_cost=false (project toggle «Согласовывать стоимость с заказчиком "
    "перед Cursor» is off), NEVER ask the customer about price, оплата, or стоимость — "
    "call submit_development_task. Only if ask_customer_about_cost=true, agree the amount "
    "and pm_record_decision with topic стоимость/cost before Cursor. "
    "If the customer/project toggle wait_estimated_duration is on, do not accept QA before "
    "the estimated minimum execution time has elapsed. If it is off, accept QA as soon as "
    "Cursor evidence is ready — do not wait out the estimate. "
    "When a customer card has tracker_project_id, the platform periodically polls ice_tracker. "
    "On a tracker backlog tick, if this run has no focused open case, call "
    "pm_poll_tracker then pm_structure_task for ONE unfinished card with "
    "context_json.tracker_task_id + tracker_project_id. Do not attach a tracker "
    "card to an unrelated open case. "
    "If the manager orders a project wipe/reset, call pm_reset_project "
    "(open PM cases only; never wipe ice_tracker history) and do not create a "
    "new development task from that order. "
    "Communicate naturally and briefly; do not expose internal JSON or raw Cursor output."
)

PHASE_PLAYBOOKS: dict[str, str] = {
    "DISCUSSION": (
        "Phase DISCUSSION: clarify, draft ТЗ, record decisions. "
        "submit_development_task only when pm_assess_execution / context.execution.verdict "
        "is execute — spec confirmed by the customer and the task has goal/requirements/"
        "acceptance criteria. You still decide whether this slice belongs in the ТЗ. "
        "Do not consult_manager to confirm ordinary ТЗ. "
        "An idea such as 'it would be nice someday' is discussion, not authorization to start work. "
        "Only an explicit request, or work allowed by the project's autonomy level, may become a "
        "development submission. Ask only the minimum missing questions. "
        "Before development, store a structured task with business context, concrete requirements, "
        "testable acceptance criteria, constraints, edge cases, dependencies, priority, and source. "
        "A small bug that you judge to be inside the confirmed ТЗ still goes without asking "
        "«можно начинать?». Structure, estimate internally, then submit_development_task only when "
        "the execution verdict is execute."
    ),
    "REQUIREMENTS_READY": (
        "Phase REQUIREMENTS_READY / CLIENT_CONFIRMED: the slice is structured. "
        "Confirm remaining decisions, then submit_development_task when the execution verdict "
        "is execute and Composer is free. Do not reopen a settled ТЗ unless the customer "
        "changes it. If Composer is busy with another job, do not submit_development_task. "
        "If Composer is idle and the leftover result belongs to another task_id, "
        "call submit_development_task now — that leftover is not this assignment. "
        "Working hours are per project. Outside hours you may discuss — but do NOT call "
        "submit_development_task / Cursor until working hours (platform defers automatically)."
    ),
    "READY_FOR_DEV": (
        "Phase READY_FOR_DEV: the gate already passed. Call submit_development_task once. "
        "If Composer is busy, wait. Do not rewrite the brief or re-ask the customer for "
        "permission. Do not consult_manager for ordinary start."
    ),
    "IN_DEVELOPMENT": (
        "Phase IN_DEVELOPMENT: a Cursor run is in flight or just finished. "
        "Call get_development_status / get_development_result. Do not submit a second prompt "
        "for the same case. Status answers come from stored task state. "
        "If Composer is idle and the leftover result belongs to another task_id, "
        "this leftover is not this assignment."
    ),
    "QA": (
        "Phase QA / CLIENT_REVIEW: a completed run exists. Call pm_accept_task — never a new "
        "Cursor prompt. Compare the result with every requirement and acceptance criterion; "
        "request a fix when a criterion failed, and call PM acceptance only after verification. "
        "Never call a blocked, failed, unknown, or unverified task done. "
        "Never message the customer with the result until pm_accept_task succeeds."
    ),
    "DONE": (
        "Phase DONE / CANCELLED: do not reopen development. Answer status from stored state. "
        "A new unrelated request needs pm_structure_task with create_new_task=true."
    ),
}


def estimate_tokens(text: str) -> int:
    return max(0, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def clip_to_tokens(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    limit = budget * CHARS_PER_TOKEN
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def normalize_phase(phase: str | None) -> str:
    name = str(phase or "DISCUSSION").strip().upper()
    return PHASE_ALIASES.get(name, "DISCUSSION")


def compile_playbook(phase: str | None = None) -> str:
    key = normalize_phase(phase)
    extra = PHASE_PLAYBOOKS.get(key) or PHASE_PLAYBOOKS["DISCUSSION"]
    return f"{SHARED_RAILS}\n\n{extra}"


def _dedupe_blocks(blocks: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for block in blocks:
        text = (block or "").strip()
        if not text:
            continue
        fingerprint = " ".join(text.lower().split()[:24])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(text)
    return out


def format_verdicts_block(verdicts: list[dict[str, Any]] | None) -> str:
    if not verdicts:
        return ""
    lines = [
        "## Judge verdicts (facts — do not re-derive, do not argue with rails)",
    ]
    for row in verdicts:
        kind = str(row.get("kind") or "judge")
        verdict = str(row.get("verdict") or "")
        confidence = row.get("confidence")
        reason = str(row.get("reasoning") or row.get("reason") or "").strip()
        conf = f" conf={float(confidence):.2f}" if confidence is not None else ""
        line = f"- {kind}: {verdict}{conf}"
        if reason:
            line += f" — {reason[:220]}"
        lines.append(line)
        evidence = row.get("evidence") or []
        for quote in evidence[:2]:
            if isinstance(quote, dict) and quote.get("text"):
                lines.append(f"  quote: {str(quote.get('text'))[:180]}")
    return "\n".join(lines)


def format_known_already_block(known: dict[str, Any] | None) -> str:
    if not known:
        return ""
    verdict = str(known.get("verdict") or "")
    if verdict not in {"known", "partially_known"}:
        return ""
    answer = str(known.get("answer") or "").strip()
    if not answer:
        return ""
    return (
        "## Already known — do not ask the customer again\n"
        f"{verdict}: {answer[:800]}"
    )


def compile_prompt(
    *,
    phase: str | None = None,
    identity_sections: list[str] | None = None,
    dossier: str = "",
    memories: str = "",
    verdicts: list[dict[str, Any]] | None = None,
    known: dict[str, Any] | None = None,
    extra_sections: list[str] | None = None,
) -> tuple[str, dict[str, int]]:
    """Return (prompt, token sizes per block)."""
    playbook = clip_to_tokens(compile_playbook(phase), TOKEN_BUDGETS["playbook"])
    identity = clip_to_tokens(
        "\n\n".join(_dedupe_blocks(list(identity_sections or []))),
        TOKEN_BUDGETS["identity"],
    )
    dossier_text = clip_to_tokens(dossier, TOKEN_BUDGETS["dossier"])
    if identity and dossier_text:
        ident_norm = identity.casefold()
        dossier_text = "\n".join(
            line
            for line in dossier_text.splitlines()
            if line.strip() and line.strip().casefold() not in ident_norm
        )
    memory_text = clip_to_tokens(memories, TOKEN_BUDGETS["memories"])
    verdict_text = clip_to_tokens(format_verdicts_block(verdicts), TOKEN_BUDGETS["verdicts"])
    known_text = clip_to_tokens(format_known_already_block(known), TOKEN_BUDGETS["known"])
    other = clip_to_tokens(
        "\n\n".join(_dedupe_blocks(list(extra_sections or []))),
        TOKEN_BUDGETS["other_sections"],
    )
    parts = [playbook, identity, dossier_text, verdict_text, known_text, memory_text, other]
    prompt = "\n\n".join(part for part in parts if part.strip())
    sizes = {
        "playbook": estimate_tokens(playbook),
        "identity": estimate_tokens(identity),
        "dossier": estimate_tokens(dossier_text),
        "verdicts": estimate_tokens(verdict_text),
        "known": estimate_tokens(known_text),
        "memories": estimate_tokens(memory_text),
        "other": estimate_tokens(other),
        "total": estimate_tokens(prompt),
    }
    logger.info("prompt_compiler sizes=%s phase=%s", sizes, normalize_phase(phase))
    return prompt, sizes
