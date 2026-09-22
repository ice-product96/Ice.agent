"""HTTP callback Cursor can POST when a task finishes (summary + status)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from .db import CursorRun, WorkItem

logger = logging.getLogger(__name__)

CALLBACK_ARG_KEYS = (
    "callbackUrl",
    "callback_url",
    "webhook",
    "webhookUrl",
    "webhook_url",
)
# "idle" is deliberately absent: an idle composer is not a finished assignment. The
# task_completed event / done flag and the cursor_completion judge decide.
_FINISHED = frozenset(
    {
        "finished",
        "completed",
        "succeeded",
        "success",
        "done",
        "complete",
    }
)
_FAILED = frozenset({"error", "failed", "failure", "cancelled", "canceled", "stopped"})


def cursor_callback_token(work_item_id: Any, secret: str) -> str:
    payload = f"cursor-callback:{work_item_id}".encode()
    key = (secret or "change-me").encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:40]


def verify_cursor_callback_token(work_item_id: Any, token: str, secret: str) -> bool:
    if not token:
        return False
    expected = cursor_callback_token(work_item_id, secret)
    return hmac.compare_digest(expected, token)


def verify_cursor_webhook_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    received = str(signature or "").strip()
    if not received or not secret:
        return False
    digest = hmac.new(
        secret.encode(),
        raw_body or b"",
        hashlib.sha256,
    ).hexdigest()
    expected = "sha256=" + digest
    if received.startswith("sha256="):
        return hmac.compare_digest(expected, received)
    return hmac.compare_digest(digest, received)


def verify_cursor_bearer_secret(secret: str, authorization: str) -> bool:
    """CursorRemote Task callback sends Authorization: Bearer <secret>."""
    received = str(authorization or "").strip()
    expected_secret = str(secret or "").strip()
    if not received or not expected_secret:
        return False
    scheme, _, token = received.partition(" ")
    if scheme.casefold() != "bearer":
        return False
    return hmac.compare_digest(token.strip(), expected_secret)


def cursor_callback_authorized(
    *,
    secret: str,
    work_item_id: Any = None,
    token: str = "",
    authorization: str = "",
    raw_body: bytes = b"",
    signature: str = "",
) -> bool:
    if work_item_id not in (None, "") and verify_cursor_callback_token(
        work_item_id, token, secret
    ):
        return True
    if verify_cursor_bearer_secret(secret, authorization):
        return True
    if verify_cursor_webhook_signature(secret, raw_body, signature):
        return True
    return False


def cursor_callback_url(
    work_item_id: Any,
    *,
    public_base_url: str,
    secret_key: str,
) -> str:
    base = str(public_base_url or "").strip().rstrip("/")
    if not base or work_item_id in (None, ""):
        return ""
    token = cursor_callback_token(work_item_id, secret_key)
    return f"{base}/api/v1/cursor/callback/{work_item_id}?token={token}"


def cursor_task_complete_url(*, public_base_url: str) -> str:
    """Global CursorRemote Task callback URL (no work_item id in path)."""
    base = str(public_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/api/v1/cursor/task-complete"


def callback_args_for_send_task(url: str) -> dict[str, str]:
    text = str(url or "").strip()
    if not text:
        return {}
    return {key: text for key in CALLBACK_ARG_KEYS}


def unknown_callback_argument(exc: BaseException) -> bool:
    blob = str(exc or "").casefold()
    mentions_callback = "callback" in blob or "webhook" in blob or any(
        key.casefold() in blob for key in CALLBACK_ARG_KEYS
    )
    schema_ish = any(
        marker in blob
        for marker in (
            "unexpected",
            "unknown",
            "extra",
            "not allowed",
            "did not match",
            "additional properties",
        )
    )
    names_field = any(
        marker in blob for marker in ("propert", "argument", "field", "parameter", "key")
    )
    return mentions_callback or (schema_ish and names_field)


def parse_cursor_callback_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def normalize_cursor_callback_payload(body: Any) -> dict[str, Any]:
    data = body if isinstance(body, dict) else {}
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    status = str(
        data.get("status")
        or task.get("status")
        or data.get("agentStatus")
        or ""
    ).strip()
    lowered = status.casefold()
    summary = str(
        data.get("summary")
        or data.get("result")
        or data.get("text")
        or task.get("summary")
        or task.get("result")
        or ""
    ).strip()
    result_text = str(data.get("result") or task.get("result") or summary).strip()
    event = str(data.get("event") or "").strip()
    done = data.get("done") is True or lowered in _FINISHED
    failed = lowered in _FAILED or (event == "statusChange" and lowered == "error")
    if event in {"task_completed", "task-complete", "taskComplete"}:
        done = True if data.get("done") is not False else done
        failed = False if done and not failed else failed
    if event == "statusChange" and lowered == "finished":
        done = True
        failed = False
    mapped = "idle" if done and not failed else ("error" if failed else (lowered or "unknown"))
    remote_id = str(
        data.get("id") or task.get("id") or data.get("taskId") or data.get("task_id") or ""
    ).strip()
    payload: dict[str, Any] = {
        "ok": not failed,
        "done": bool(done or failed),
        "status": mapped,
        "summary": summary or result_text,
        "result": result_text or summary,
        "started": True,
        "seen_busy": True,
        "prompt_sent": False,
        "callback": True,
        "cursor_remote_task_id": remote_id,
        "cursor_composer_id": str(data.get("composerId") or data.get("composer_id") or "").strip(),
        "cursor_window_id": str(data.get("windowId") or data.get("window_id") or "").strip(),
        "workspace": str(
            data.get("workspacePath") or data.get("workspace") or data.get("workspaceName") or ""
        ).strip(),
        "raw": data,
    }
    if failed:
        payload["result"] = {
            "task_id": str(data.get("taskId") or data.get("task_id") or ""),
            "status": "failed",
            "implementation": {
                "summary": summary or result_text or "Cursor reported an error",
                "files_changed": [],
                "tests": [],
            },
            "verification": {
                "tests_passed": False,
                "lint_passed": False,
                "acceptance_criteria": [],
            },
            "questions": [],
            "risks": [summary or result_text or "Cursor error"],
            "limitations": ["Reported via completion callback"],
        }
    return payload


async def apply_cursor_callback(
    db: Any,
    item: WorkItem,
    payload: dict[str, Any],
    *,
    judgment: Any = None,
    client: Any = None,
) -> dict[str, Any]:
    from .pm_state import get_or_create_cursor_run
    from .runtime import _apply_pm_cursor_result
    from .cursorremote_drive import remember_cursor_worker

    if item.pm_phase in {"QA", "CLIENT_REVIEW", "DONE", "CANCELLED"} or item.status == "done":
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_finished",
            "work_item_id": item.id,
            "pm_phase": item.pm_phase,
        }
    if not payload.get("done") and str(payload.get("status") or "") not in _FAILED:
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_terminal",
            "work_item_id": item.id,
            "status": payload.get("status"),
        }
    run = (
        await db.get(CursorRun, item.active_cursor_run_id)
        if item.active_cursor_run_id
        else None
    )
    if run is None or run.status not in {"pending", "running"}:
        run, _created = await get_or_create_cursor_run(
            db,
            item,
            attempt=max(int(getattr(run, "attempt", 0) or 0), 0) + 1,
            request={"source": "cursor_callback"},
        )
        item.active_cursor_run_id = run.id
    remember_cursor_worker(item, payload)
    return await _apply_pm_cursor_result(
        db, item, run, payload, judgment=judgment, client=client
    )


def _work_item_id_from_callback_body(body: dict[str, Any]) -> int | None:
    for key in ("work_item_id", "workItemId", "taskId", "task_id", "case_id", "caseId"):
        raw = body.get(key)
        if raw in (None, ""):
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    prompt = str(body.get("prompt") or "")
    for marker in ("кейс #", "case #", "work_item_id=", "task_id="):
        idx = prompt.casefold().find(marker.casefold())
        if idx < 0:
            continue
        tail = prompt[idx + len(marker) :]
        digits = []
        for ch in tail.lstrip():
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            try:
                return int("".join(digits))
            except ValueError:
                return None
    return None


async def resolve_work_item_for_task_callback(
    db: Any,
    body: dict[str, Any],
) -> WorkItem | None:
    """Match CursorRemote global task_completed POST to an in-flight ice.agent case."""
    from sqlalchemy import select

    from .db import Customer
    from .cursorremote_drive import normalize_workspace_path, workspace_matches

    explicit_id = _work_item_id_from_callback_body(body)
    if explicit_id is not None:
        item = await db.get(WorkItem, explicit_id)
        if item is not None and item.pm_phase not in {"DONE", "CANCELLED"}:
            return item

    composer = str(body.get("composerId") or body.get("composer_id") or "").strip()
    window_id = str(body.get("windowId") or body.get("window_id") or "").strip()
    workspace = str(
        body.get("workspacePath")
        or body.get("workspace")
        or body.get("workspaceName")
        or ""
    ).strip()

    candidates = list(
        await db.scalars(
            select(WorkItem)
            .where(
                WorkItem.status.in_(("waiting_external", "in_progress")),
                WorkItem.pm_phase.notin_(("DONE", "CANCELLED")),
            )
            .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
            .limit(40)
        )
    )
    active = [
        item
        for item in candidates
        if item.active_cursor_run_id
        or bool((item.metadata_json or {}).get("cursor_in_flight"))
        or item.status == "waiting_external"
    ]
    pool = active or candidates
    if not pool:
        return None

    def meta_of(item: WorkItem) -> dict[str, Any]:
        return item.metadata_json if isinstance(item.metadata_json, dict) else {}

    if composer:
        for item in pool:
            if str(meta_of(item).get("cursor_composer_id") or "") == composer:
                return item

    customers = list(await db.scalars(select(Customer)))
    customer_by_id = {row.id: row for row in customers}
    customer_by_project = {
        str(row.project_id or "").strip(): row
        for row in customers
        if (row.project_id or "").strip()
    }

    def customer_for(item: WorkItem) -> Any | None:
        if item.customer_id and item.customer_id in customer_by_id:
            return customer_by_id[item.customer_id]
        if item.project_id:
            return customer_by_project.get(str(item.project_id))
        return None

    if window_id:
        for item in pool:
            meta = meta_of(item)
            if str(meta.get("cursor_window_id") or "") == window_id:
                return item
            customer = customer_for(item)
            if customer is not None and str(customer.cursor_window_id or "") == window_id:
                return item

    if workspace:
        want = normalize_workspace_path(workspace)
        want_name = want.rsplit("/", 1)[-1] if want else ""
        scored: list[tuple[int, WorkItem]] = []
        for item in pool:
            score = 0
            meta = meta_of(item)
            item_ws = str(meta.get("cursor_workspace") or "").strip()
            if item_ws and workspace_matches(want, [item_ws]):
                score += 3
            customer = customer_for(item)
            if customer is not None:
                if workspace_matches(want, [str(customer.cursor_workspace or "")]):
                    score += 3
                if window_id and str(customer.cursor_window_id or "") == window_id:
                    score += 2
            project_name = normalize_workspace_path(str(item.project_id or "")).rsplit("/", 1)[-1]
            if want_name and want_name == project_name:
                score += 1
            if score:
                scored.append((score, item))
        if scored:
            scored.sort(key=lambda pair: (-pair[0], -(pair[1].id or 0)))
            return scored[0][1]

    if len(pool) == 1:
        return pool[0]
    for item in pool:
        if item.status == "waiting_external" and item.active_cursor_run_id:
            return item
    return pool[0]
