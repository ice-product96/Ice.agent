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

# Anything not clearly idle is treated as in-flight. Search/explore used to look
# "stopped" because it was missing from this set — the agent then sent a duplicate prompt.
BUSY_STATUSES = frozenset({
    "thinking",
    "generating",
    "running_tool",
    "waiting_approval",
    "searching",
    "exploring",
    "planning",
    "reading",
    "applying",
    "editing",
    "compiling",
    "indexing",
    "streaming",
    "working",
    "running",
})
IDLE_STATUSES = frozenset({"idle", "ready", "done", "complete", "completed"})
STOPPED_STATUSES = frozenset({"error", "failed", "cancelled", "canceled", "stopped"})

CURSOR_CHECK_ONLY_MESSAGE = (
    "Только cursorremote_check. Не вызывай cursorremote_do и не давай Cursor новую задачу "
    "(даже если в сводке «поиск» или кажется, что он остановился). "
    "Пока done=false — он ещё работает: снова schedule_self через ~2 минуты. "
    "Если done=true — итог заказчику, новый промпт не отправляй."
)

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

WORKSPACE_UNAVAILABLE_HINT = (
    "Required Cursor workspace is not open. Open that project folder in Cursor on the MCP host, "
    "then retry submit_development_task / cursorremote_do. Do not leave the case waiting on Cursor."
)

CURSOR_UNAVAILABLE_HINT = (
    "CursorRemote MCP has no usable Cursor window. Start Cursor on the MCP host and open the "
    "project workspace, then retry. Do not leave the case waiting on Cursor."
)


def normalize_workspace_path(path: str | None) -> str:
    raw = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not raw:
        return ""
    if len(raw) >= 2 and raw[1] == ":":
        raw = raw[0].lower() + raw[1:]
    return raw.lower()


def workspace_paths_from_status(status: Any) -> list[str]:
    data = _as_status_dict(status) or {}
    found: list[str] = []

    def add(value: Any) -> None:
        text = normalize_workspace_path(str(value or ""))
        if text and text not in found:
            found.append(text)

    for key in (
        "workspacePath",
        "workspace_path",
        "workspaceFolder",
        "workspace",
        "workspaceUri",
        "folderUri",
        "path",
    ):
        add(data.get(key))
    for window in list(data.get("windows") or data.get("targets") or []):
        if not isinstance(window, dict):
            continue
        for key in (
            "workspacePath",
            "workspace_path",
            "workspaceFolder",
            "workspace",
            "workspaceUri",
            "folderUri",
            "path",
        ):
            add(window.get(key))
    return found


def workspace_matches(expected: str | None, candidates: list[str]) -> bool:
    want = normalize_workspace_path(expected)
    if not want:
        return True
    for candidate in candidates:
        got = normalize_workspace_path(candidate)
        if not got:
            continue
        if got == want or got.endswith("/" + want) or want.endswith("/" + got):
            return True
        # Compare by last path segment (uraltrade).
        if got.rstrip("/").split("/")[-1] == want.rstrip("/").split("/")[-1]:
            return True
    return False


def prompt_actually_started(result: dict[str, Any] | None) -> bool:
    """True when Cursor accepted work for this assignment (or was already busy with it)."""
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {
        "workspace_unavailable",
        "cursor_unavailable",
        "not_started",
        "no_window",
    }:
        return False
    # Already-busy Composer: we skipped a new prompt on purpose — still in flight.
    if result.get("seen_busy") or result.get("started") or status in BUSY_STATUSES:
        return True
    if result.get("done") and (result.get("prompt_sent") or result.get("seen_busy")):
        return True
    if result.get("skipped_prompt") or not result.get("prompt_sent"):
        return False
    return True


async def ensure_cursor_workspace(
    session: Any,
    *,
    expected_workspace: str | None = None,
    expected_window_id: str | None = None,
) -> dict[str, Any]:
    """Verify Cursor is reachable and optionally on the expected project folder."""
    status: Any = None
    try:
        status = await mcp_call(session, "get_status")
    except Exception as exc:
        return {
            "ok": False,
            "status": "cursor_unavailable",
            "reason": f"CursorRemote get_status failed: {exc}",
            "hint": CURSOR_UNAVAILABLE_HINT,
            "workspace": None,
            "windows": [],
        }
    paths = workspace_paths_from_status(status)
    if not paths:
        try:
            listed = await mcp_call(session, "list_windows")
            paths = workspace_paths_from_status(listed) or workspace_paths_from_status(
                {"windows": listed if isinstance(listed, list) else [listed]}
            )
        except Exception:
            listed = None
    if expected_window_id:
        try:
            await mcp_call(
                session,
                "switch_window",
                {"windowId": expected_window_id, "id": expected_window_id},
            )
            status = await mcp_call(session, "get_status")
            paths = workspace_paths_from_status(status) or paths
        except Exception as exc:
            logger.info("CursorRemote switch_window failed: %s", exc)
    if expected_workspace and not workspace_matches(expected_workspace, paths):
        # Try switching by matching path from list_windows if available.
        try:
            listed = await mcp_call(session, "list_windows")
            windows = listed if isinstance(listed, list) else (
                (listed or {}).get("windows") if isinstance(listed, dict) else []
            )
            for window in windows or []:
                if not isinstance(window, dict):
                    continue
                window_paths = workspace_paths_from_status(window)
                if not workspace_matches(expected_workspace, window_paths):
                    continue
                window_id = (
                    window.get("id")
                    or window.get("windowId")
                    or window.get("targetId")
                )
                if not window_id:
                    continue
                try:
                    await mcp_call(
                        session,
                        "switch_window",
                        {"windowId": window_id, "id": window_id},
                    )
                    status = await mcp_call(session, "get_status")
                    paths = workspace_paths_from_status(status) or window_paths
                    break
                except Exception as exc:
                    logger.info("CursorRemote switch_window by path failed: %s", exc)
        except Exception:
            pass
    if expected_workspace and not workspace_matches(expected_workspace, paths):
        return {
            "ok": False,
            "status": "workspace_unavailable",
            "reason": (
                f"Cursor workspace «{expected_workspace}» is not open. "
                f"Open windows: {paths or ['(none)']}."
            ),
            "hint": WORKSPACE_UNAVAILABLE_HINT,
            "workspace": expected_workspace,
            "windows": paths,
            "last": status,
        }
    if not paths and expected_workspace:
        return {
            "ok": False,
            "status": "workspace_unavailable",
            "reason": (
                f"No open Cursor window for workspace «{expected_workspace}»."
            ),
            "hint": WORKSPACE_UNAVAILABLE_HINT,
            "workspace": expected_workspace,
            "windows": [],
            "last": status,
        }
    return {
        "ok": True,
        "status": "ready",
        "workspace": paths[0] if paths else expected_workspace,
        "windows": paths,
        "last": status,
    }


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
    if name in STOPPED_STATUSES:
        return False
    if name in IDLE_STATUSES or name in {"", "unknown"}:
        return False
    if name in BUSY_STATUSES:
        return True
    return True


def cursor_has_active_work(state: Any) -> bool:
    """True if the chat already shows search/tools/plan — not a blank idle editor."""
    data = parse_mcp_payload(state)
    if not isinstance(data, dict):
        return False
    if data.get("pendingApprovals"):
        return True
    for item in list(data.get("messages") or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        if kind in {"assistant", "plan", "tool", "tool_call", "thinking", "search"}:
            return True
        if kind == "human":
            continue
        if str(item.get("text") or "").strip():
            return True
    return False


def should_pin_cursor_followup(message: str) -> bool:
    text = (message or "").lower()
    if not text:
        return True
    markers = (
        "cursor",
        "cursorremote",
        "остановил",
        "stopped",
        "searching",
        "поиск",
        "explore",
        "ide",
    )
    return any(token in text for token in markers)


def pin_cursor_followup_message(message: str) -> str:
    if should_pin_cursor_followup(message):
        return CURSOR_CHECK_ONLY_MESSAGE
    return message


def is_cursor_poll_followup(payload: Any) -> bool:
    """True for a scheduled check-only Cursor follow-up."""
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("source") or "") == "scheduled"
        and str(payload.get("message") or "").strip() == CURSOR_CHECK_ONLY_MESSAGE
        and payload.get("work_item_id") not in (None, "", False)
    )


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
                # Keep enough of the final JSON for PM parsing (CursorRemote often
                # already clips each message; avoid clipping it again to a tiny stub).
                chunks.append(text[:12000])
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
        "seen_busy": seen_busy,
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
            try:
                peek_state = await mcp_call(session, "get_state", {"messageLimit": 8})
            except Exception:
                peek_state = None
            if cursor_has_active_work(peek_state):
                seen_busy = True
                await asyncio.sleep(2)
                continue
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
    attachments: list[dict[str, Any]] | None = None,
    work_item_id: Any = None,
    public_base_url: str = "",
    secret_key: str = "",
    expected_workspace: str | None = None,
    expected_window_id: str | None = None,
) -> dict[str, Any]:
    ensure = await ensure_cursor_workspace(
        session,
        expected_workspace=expected_workspace,
        expected_window_id=expected_window_id,
    )
    if not ensure.get("ok"):
        return {
            "ok": False,
            "done": False,
            "sent": False,
            "prompt_sent": False,
            "started": False,
            "seen_busy": False,
            "status": ensure.get("status") or "workspace_unavailable",
            "reason": ensure.get("reason"),
            "summary": ensure.get("reason") or "",
            "next": ensure.get("hint") or WORKSPACE_UNAVAILABLE_HINT,
            "workspace": ensure.get("workspace"),
            "windows": ensure.get("windows") or [],
            "last": ensure.get("last"),
        }
    try:
        current = await mcp_call(session, "get_status")
    except Exception:
        current = ensure.get("last")
    if cursor_is_busy(current):
        driven = await drive_until_done(
            session,
            timeout_ms=timeout_ms,
            require_busy=False,
            start_grace_ms=0,
        )
        return {
            **driven,
            "sent": False,
            "prompt_sent": False,
            "skipped_prompt": True,
            "reason": (
                "Cursor already working — did not send a duplicate task. "
                "Polled the current job instead."
            ),
        }
    from .cursor_file_transfer import build_customer_files_prompt, deliver_customer_files_to_cursor

    delivery = await deliver_customer_files_to_cursor(
        session,
        attachments,
        work_item_id=work_item_id,
        public_base_url=public_base_url,
        secret_key=secret_key,
    )
    prompt = build_customer_files_prompt(
        text,
        workspace_paths=delivery.get("paths") or [],
        download_steps=delivery.get("download_steps") or [],
        inline_note=str(delivery.get("inline_note") or ""),
    )
    send_payload: dict[str, Any] = {"text": prompt}
    inline_attachments = delivery.get("send_prompt_attachments") or []
    if inline_attachments:
        send_payload["attachments"] = inline_attachments
    try:
        sent = await mcp_call(session, "send_prompt", send_payload)
    except Exception as exc:
        return {
            "ok": False,
            "done": False,
            "sent": False,
            "prompt_sent": False,
            "started": False,
            "seen_busy": False,
            "status": "cursor_unavailable",
            "reason": f"send_prompt failed: {exc}",
            "summary": f"send_prompt failed: {exc}",
            "next": CURSOR_UNAVAILABLE_HINT,
            "workspace": ensure.get("workspace"),
            "windows": ensure.get("windows") or [],
        }
    driven = await drive_until_done(session, timeout_ms=timeout_ms, require_busy=True)
    result = {
        "sent": sent,
        "prompt_sent": True,
        **driven,
        "file_delivery": {
            "method": delivery.get("method"),
            "paths": delivery.get("paths") or [],
        },
    }
    if delivery.get("paths"):
        result["images"] = delivery["paths"]
    # Prompt API accepted, but Composer never became busy → treat as not delivered.
    if (
        not result.get("done")
        and not result.get("seen_busy")
        and str(result.get("status") or "") == "not_started"
    ):
        result.update(
            {
                "ok": False,
                "prompt_sent": False,
                "started": False,
                "reason": (
                    "Cursor did not start after send_prompt — workspace/window likely wrong "
                    "or Composer did not accept the task."
                ),
                "next": NOT_STARTED_HINT,
            }
        )
    return result


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
