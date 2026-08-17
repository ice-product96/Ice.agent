"""Drive CursorRemote: send work, click Allow/Accept, wait until Cursor actually finishes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

APPROVE_LABELS = (
    "allow",
    "accept",
    "approve",
    "run",
    "accept all",
    "allowlist",
)

BUSY_STATUSES = frozenset({"thinking", "generating", "running_tool", "waiting_approval"})

FOLLOW_UP_HINT = (
    "Cursor is not finished. Call schedule_self in about 2 minutes with a message to run "
    "cursorremote_check, keep waiting while done=false, and only telegram the customer after "
    "done=true AND you verified the summary. Never tell the customer the work is ready while "
    "done=false or after a mere send_prompt."
)

DONE_HINT = (
    "Cursor finished (done=true). Do NOT call schedule_self again. "
    "Write a short result to the person who asked for this work in the original Telegram chat. "
    "If summary is empty, still report that Cursor is idle and the task is complete."
)

NOT_STARTED_HINT = (
    "Cursor did not start working after the prompt. Check get_status / workspace, retry "
    "cursorremote_do once if needed, or schedule_self to retry. Do not tell the customer it is done."
)


def parse_mcp_payload(content: Any) -> Any:
    if isinstance(content, dict) and "text" in content:
        text = content.get("text")
        if isinstance(text, str):
            stripped = text.strip()
            if stripped[:1] in "{[":
                try:
                    return parse_mcp_payload(json.loads(stripped))
                except json.JSONDecodeError:
                    pass
            if len(content) <= 3:
                return text
    if isinstance(content, list):
        if len(content) == 1:
            return parse_mcp_payload(content[0])
        return [parse_mcp_payload(item) for item in content]
    if isinstance(content, str):
        stripped = content.strip()
        if stripped[:1] in "{[":
            try:
                return parse_mcp_payload(json.loads(stripped))
            except json.JSONDecodeError:
                return content
        return content
    return content


async def mcp_call(session: Any, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    response = await session.call_tool(tool, dict(arguments or {}))
    content = [item.model_dump() for item in response.content]
    if getattr(response, "isError", False):
        detail = "; ".join(str(item.get("text") or item) for item in content)
        raise RuntimeError(detail or f"MCP tool {tool} failed")
    return parse_mcp_payload(content)


def _as_status_dict(status: Any) -> dict[str, Any] | None:
    data = parse_mcp_payload(status)
    if isinstance(data, list) and data:
        data = parse_mcp_payload(data[0])
    return data if isinstance(data, dict) else None


def cursor_is_busy(status: Any) -> bool:
    data = _as_status_dict(status)
    if not data:
        return False
    if int(data.get("pendingApprovalCount") or 0) > 0:
        return True
    if data.get("agentActivityLive"):
        return True
    name = str(data.get("agentStatus") or data.get("status") or "").strip().lower()
    return name in BUSY_STATUSES


def summarize_cursor_state(state: Any) -> str:
    if not isinstance(state, dict):
        return ""
    chunks: list[str] = []
    for item in list(state.get("messages") or [])[-8:]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "assistant":
            text = str(item.get("text") or "").strip()
            if text:
                chunks.append(text[:2000])
        elif kind == "plan":
            label = str(item.get("label") or "plan")
            desc = str(item.get("description") or "").strip()
            todos = f"{item.get('todosCompleted') or 0}/{item.get('todosTotal') or 0}"
            chunks.append(f"[{label} {todos}] {desc}"[:1500])
    return "\n---\n".join(chunks[-4:])


def _status_name(status: Any) -> str:
    if not isinstance(status, dict):
        return "unknown"
    name = str(status.get("agentStatus") or status.get("status") or "").strip().lower()
    return name or "unknown"


def _approval_actions(pending: list[Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        for action in item.get("actions") or []:
            if not isinstance(action, dict):
                continue
            label = str(action.get("label") or action.get("type") or "").lower()
            selector = str(action.get("selectorPath") or action.get("selector") or "").strip()
            kind = str(action.get("type") or "").lower()
            if kind in {"reject", "deny", "skip"}:
                continue
            if kind in {"approve", "run"} or any(token in label for token in APPROVE_LABELS):
                if selector:
                    actions.append({"label": action.get("label") or kind, "selectorPath": selector})
    return actions


async def click_pending_approvals(session: Any) -> list[dict[str, Any]]:
    """Click Allow/Accept/Run/Accept All for whatever Cursor is waiting on."""
    clicked: list[dict[str, Any]] = []
    try:
        status = await mcp_call(session, "get_status")
    except Exception as exc:
        logger.info("CursorRemote get_status failed: %s", exc)
        return clicked
    if not isinstance(status, dict):
        return clicked
    count = int(status.get("pendingApprovalCount") or 0)
    agent_status = str(status.get("agentStatus") or "")
    if count <= 0 and agent_status != "waiting_approval":
        return clicked

    try:
        all_result = await mcp_call(session, "approve_all")
        clicked.append({"tool": "approve_all", "result": all_result})
    except Exception as exc:
        logger.info("CursorRemote approve_all failed: %s", exc)

    try:
        state = await mcp_call(session, "get_state", {"messageLimit": 6})
    except Exception as exc:
        logger.info("CursorRemote get_state failed: %s", exc)
        return clicked
    pending = []
    if isinstance(state, dict):
        pending = list(state.get("pendingApprovals") or [])
    for action in _approval_actions(pending):
        selector = action["selectorPath"]
        try:
            result = await mcp_call(session, "approve", {"selectorPath": selector})
            clicked.append({"tool": "approve", "selectorPath": selector, "result": result})
        except Exception:
            try:
                result = await mcp_call(
                    session,
                    "click_action",
                    {"selectorPath": selector, "actionLabel": str(action.get("label") or "Allow")},
                )
                clicked.append({"tool": "click_action", "selectorPath": selector, "result": result})
            except Exception as exc:
                logger.info("CursorRemote click failed %s: %s", selector[:80], exc)
    return clicked


async def _snapshot(session: Any) -> tuple[Any, Any, str]:
    status: Any = None
    state: Any = None
    try:
        status = await mcp_call(session, "get_status")
    except Exception as exc:
        logger.info("CursorRemote get_status failed: %s", exc)
    try:
        state = await mcp_call(session, "get_state", {"messageLimit": 8})
    except Exception as exc:
        logger.info("CursorRemote get_state failed: %s", exc)
    return status, state, summarize_cursor_state(state)


def _result(
    *,
    done: bool,
    status: str,
    last: Any,
    state: Any,
    summary: str,
    approvals: list[dict[str, Any]],
    seen_busy: bool,
    hint: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "done": done,
        "status": status,
        "started": seen_busy,
        "summary": summary,
        "approvals": approvals,
        "last": last,
        "messages": (state or {}).get("messages") if isinstance(state, dict) else [],
    }
    payload["next"] = DONE_HINT if done else (hint or FOLLOW_UP_HINT)
    return payload


async def drive_until_done(
    session: Any,
    *,
    timeout_ms: int = 90_000,
    start_grace_ms: int = 25_000,
    idle_debounce_ms: int = 3_000,
    require_busy: bool = True,
) -> dict[str, Any]:
    """Poll Cursor until it has actually worked and then gone idle.

    Immediate idle is not treated as completion: Cursor often looks idle right after
    send_prompt, before thinking starts. Long coding jobs should return done=false
    so the employee schedules a follow-up instead of telling the customer it is ready.
    """
    approvals: list[dict[str, Any]] = []
    seen_busy = False
    last: Any = None
    deadline = time.monotonic() + max(1, timeout_ms) / 1000
    start = time.monotonic()

    while time.monotonic() < deadline:
        approvals.extend(await click_pending_approvals(session))
        try:
            last = await mcp_call(session, "get_status")
        except Exception as exc:
            last = {"error": str(exc)}
            await asyncio.sleep(2)
            continue

        if cursor_is_busy(last):
            seen_busy = True
            try:
                last = await mcp_call(
                    session,
                    "wait",
                    {"for": "needs_input", "timeoutMs": min(12_000, timeout_ms)},
                )
            except Exception:
                await asyncio.sleep(2)
            continue

        if require_busy and not seen_busy:
            if (time.monotonic() - start) * 1000 < start_grace_ms:
                await asyncio.sleep(2)
                continue
            status, state, summary = await _snapshot(session)
            return _result(
                done=False,
                status="not_started",
                last=status or last,
                state=state,
                summary=summary,
                approvals=approvals,
                seen_busy=False,
                hint=NOT_STARTED_HINT,
            )

        debounce_s = max(0, idle_debounce_ms) / 1000
        if debounce_s:
            await asyncio.sleep(debounce_s)
        try:
            confirm = await mcp_call(session, "get_status")
        except Exception:
            confirm = last
        if cursor_is_busy(confirm):
            seen_busy = True
            last = confirm
            continue

        status, state, summary = await _snapshot(session)
        if cursor_is_busy(status):
            seen_busy = True
            last = status
            continue
        return _result(
            done=True,
            status="idle",
            last=status or confirm,
            state=state,
            summary=summary,
            approvals=approvals,
            seen_busy=seen_busy or not require_busy,
            hint=None,
        )

    status, state, summary = await _snapshot(session)
    name = "working" if cursor_is_busy(status or last) else _status_name(status or last)
    if name in {"idle", "unknown"} and seen_busy:
        name = "timeout"
    return _result(
        done=False,
        status=name,
        last=status or last,
        state=state,
        summary=summary,
        approvals=approvals,
        seen_busy=seen_busy,
        hint=FOLLOW_UP_HINT,
    )


async def drive_until_idle(
    session: Any,
    *,
    timeout_ms: int = 90_000,
    max_rounds: int = 12,
) -> dict[str, Any]:
    """Back-compat wrapper. Prefer drive_until_done — idle is not success by itself."""
    del max_rounds
    return await drive_until_done(session, timeout_ms=timeout_ms)


async def send_prompt_and_drive(
    session: Any,
    text: str,
    *,
    timeout_ms: int = 90_000,
) -> dict[str, Any]:
    sent = await mcp_call(session, "send_prompt", {"text": text})
    driven = await drive_until_done(session, timeout_ms=timeout_ms, require_busy=True)
    return {"sent": sent, "prompt_sent": True, **driven}


async def check_and_drive(
    session: Any,
    *,
    timeout_ms: int = 90_000,
    idle_debounce_ms: int = 3_000,
) -> dict[str, Any]:
    """Poll an already-running Cursor job. Idle without prior activity is a real finish here."""
    return await drive_until_done(
        session,
        timeout_ms=timeout_ms,
        require_busy=False,
        start_grace_ms=0,
        idle_debounce_ms=idle_debounce_ms,
    )
