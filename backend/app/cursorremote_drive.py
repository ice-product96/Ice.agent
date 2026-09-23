"""Drive CursorRemote: send work, click Allow/Accept, wait until Cursor actually finishes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

WORKER_TOOLS = frozenset({"create_session", "send_task", "get_task"})
WORKER_TASK_STATUS_MAP = {
    "succeeded": "idle",
    "completed": "idle",
    "success": "idle",
    "waiting_approval": "waiting_approval",
    "failed": "error",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "running": "generating",
}

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

_INCOMPLETE_SUMMARY_RE = re.compile(
    r"(?is)(?:"
    r"\b(?:запускаю|запущу|проверю|проверяю|смотрю|найду|сниму|сначала)\b"
    r"|i['’]ll\b|let me\b|going to\b|looking at\b"
    r")"
)

# Legacy text heuristics stay on until the cursor_completion judge is enforced; the
# judgment service flips this flag from RuntimeSettings.judge_modes.
_TEXT_HEURISTICS_ENABLED = True


def set_text_heuristics(enabled: bool) -> None:
    global _TEXT_HEURISTICS_ENABLED
    _TEXT_HEURISTICS_ENABLED = bool(enabled)


def text_heuristics_enabled() -> bool:
    return _TEXT_HEURISTICS_ENABLED


def summary_looks_incomplete(text: str) -> bool:
    """DEPRECATED regex on the summary; disabled when the completion judge is enforced."""
    if not _TEXT_HEURISTICS_ENABLED:
        return False
    return bool(_INCOMPLETE_SUMMARY_RE.search(str(text or "").strip()))

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
    "Cursor finished (done=true) and posted a summary. Do NOT call schedule_self again. "
    "Use that summary as the result for the original Telegram chat."
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


def log_cursor_stage(stage: str, *, work_item_id: Any = None, **fields: Any) -> None:
    payload = {key: value for key, value in fields.items() if value is not None}
    if work_item_id is not None:
        payload["work_item_id"] = work_item_id
    try:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        blob = str(payload)
    logger.info("cursor.%s %s", stage, blob[:4000])


def status_snapshot(status: Any) -> dict[str, Any]:
    data = _as_status_dict(status) or {}
    paths = workspace_paths_from_status(data)
    return {
        "agentStatus": data.get("agentStatus") or data.get("status") or "",
        "pendingApprovalCount": int(data.get("pendingApprovalCount") or 0),
        "agentActivityLive": bool(data.get("agentActivityLive")),
        "workspace": paths[0] if paths else None,
    }


def cursor_is_explicitly_busy(status: Any) -> bool:
    """True only for a live Composer job, not idle leftover or unknown MCP shapes."""
    data = _as_status_dict(status)
    if not data:
        return False
    if int(data.get("pendingApprovalCount") or 0) > 0:
        return True
    name = str(data.get("agentStatus") or data.get("status") or "").strip().lower()
    if name in IDLE_STATUSES or name in STOPPED_STATUSES or name in {"", "unknown"}:
        return False
    if name in BUSY_STATUSES:
        return True
    return bool(data.get("agentActivityLive"))


def prompt_actually_started(result: dict[str, Any] | None) -> bool:
    """True when THIS assignment's prompt was accepted by Composer."""
    if not isinstance(result, dict):
        return False
    if result.get("skipped_prompt") and not result.get("prompt_sent"):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {
        "workspace_unavailable",
        "cursor_unavailable",
        "not_started",
        "no_window",
        "cursor_busy",
    }:
        return False
    if result.get("prompt_sent") and (
        result.get("seen_busy") or result.get("started") or result.get("done")
    ):
        return True
    if result.get("prompt_sent") and status in BUSY_STATUSES:
        return True
    return False


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


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _omit_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


async def list_mcp_tool_names(session: Any) -> set[str]:
    try:
        result = await session.list_tools()
        return {str(item.name) for item in list(getattr(result, "tools", None) or [])}
    except Exception as exc:
        logger.info("CursorRemote list_tools failed: %s", exc)
        return set()


def worker_tools_available(names: set[str] | None) -> bool:
    return WORKER_TOOLS <= set(names or ())


def cursor_worker_kwargs(item: Any | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """IDs ice.agent should send to CursorRemote worker tools."""
    kwargs: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    if item is not None:
        meta = item.metadata_json if isinstance(getattr(item, "metadata_json", None), dict) else {}
        session_id = _first_text(meta.get("cursor_session_id"))
        remote_task_id = _first_text(meta.get("cursor_remote_task_id"))
        if session_id:
            kwargs["session_id"] = session_id
        if remote_task_id:
            kwargs["remote_task_id"] = remote_task_id
        task_id = _first_text(getattr(item, "id", None))
        if task_id:
            kwargs["task_id"] = task_id
        chat_id = _first_text(getattr(item, "chat_id", None))
        if chat_id:
            kwargs["chat_id"] = chat_id
        project_id = _first_text(getattr(item, "project_id", None))
        if project_id:
            kwargs["project_id"] = project_id
    if not kwargs.get("chat_id") and isinstance(context, dict):
        chat = _first_text(
            context.get("chat_id"),
            context.get("reply_chat_id"),
            context.get("conversation_id"),
        )
        if chat:
            kwargs["chat_id"] = chat
    if not kwargs.get("project_id") and isinstance(context, dict):
        project_id = _first_text(context.get("project_id"))
        if project_id:
            kwargs["project_id"] = project_id
    if not kwargs.get("task_id") and isinstance(context, dict):
        task_id = _first_text(context.get("work_item_id"), context.get("task_id"))
        if task_id:
            kwargs["task_id"] = task_id
    return kwargs


def remember_cursor_worker(item: Any | None, result: dict[str, Any] | None) -> None:
    if item is None or not isinstance(result, dict):
        return
    meta = dict(getattr(item, "metadata_json", None) or {})
    changed = False
    for key, sources in (
        ("cursor_session_id", ("cursor_session_id", "session_id")),
        ("cursor_remote_task_id", ("cursor_remote_task_id", "remote_task_id")),
        ("cursor_composer_id", ("cursor_composer_id", "composerId", "composer_id")),
        ("cursor_window_id", ("cursor_window_id", "windowId", "window_id")),
        ("cursor_workspace", ("cursor_workspace", "workspace", "workspacePath")),
    ):
        value = _first_text(*(result.get(name) for name in sources))
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        if not value and raw:
            value = _first_text(*(raw.get(name) for name in sources))
        if value and meta.get(key) != value:
            meta[key] = value
            changed = True
    if changed:
        item.metadata_json = meta


def _worker_task_dict(payload: Any) -> dict[str, Any]:
    data = parse_mcp_payload(payload)
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def status_from_worker_task(payload: Any) -> dict[str, Any] | None:
    data = parse_mcp_payload(payload)
    if not isinstance(data, dict):
        return None
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    raw_status = _first_text(
        data.get("agentStatus"),
        task.get("status"),
        data.get("status"),
    ).lower()
    mapped = WORKER_TASK_STATUS_MAP.get(raw_status, raw_status)
    pending = int(data.get("pendingApprovalCount") or 0)
    if data.get("needsInput"):
        pending = max(pending, 1)
        mapped = mapped or "waiting_approval"
    live = bool(data.get("agentActivityLive"))
    if data.get("done") is True and not data.get("needsInput"):
        mapped = "idle"
        live = False
    return {
        "agentStatus": mapped or "unknown",
        "pendingApprovalCount": pending,
        "agentActivityLive": live,
        "done": bool(data.get("done")),
        "needsInput": bool(data.get("needsInput") or pending),
        "summary": data.get("summary") or task.get("summary") or "",
        "result": data.get("result") or task.get("result") or "",
        "task": task,
        "connected": data.get("connected"),
        "extractorStatus": data.get("extractorStatus"),
        "hasQuestionnaire": data.get("hasQuestionnaire"),
        "files": data.get("files") or [],
    }


async def _poll_cursor_status(
    session: Any,
    *,
    remote_task_id: str | None = None,
) -> Any:
    if remote_task_id:
        try:
            payload = await mcp_call(
                session,
                "get_task",
                {"taskId": remote_task_id, "task_id": remote_task_id},
            )
            mapped = status_from_worker_task(payload)
            if mapped is not None:
                return mapped
        except Exception as exc:
            logger.info("CursorRemote get_task failed: %s", exc)
    return await mcp_call(session, "get_status")


def _worker_ids_from_create(payload: Any) -> dict[str, str]:
    data = parse_mcp_payload(payload)
    session_blob = data.get("session") if isinstance(data, dict) else None
    if not isinstance(session_blob, dict):
        session_blob = data if isinstance(data, dict) else {}
    return {
        "cursor_session_id": _first_text(session_blob.get("id")),
        "cursor_composer_id": _first_text(
            session_blob.get("composerId"),
            session_blob.get("composer_id"),
        ),
        "cursor_window_id": _first_text(
            session_blob.get("workspaceId"),
            session_blob.get("workspace_id"),
            session_blob.get("windowId"),
            session_blob.get("window_id"),
        ),
        "cursor_workspace": _first_text(
            session_blob.get("workspacePath"),
            session_blob.get("workspace_path"),
            session_blob.get("workspace"),
        ),
    }


def _worker_ids_from_task(payload: Any, *, session_id: str = "") -> dict[str, str]:
    task = _worker_task_dict(payload)
    return {
        "cursor_session_id": _first_text(
            task.get("sessionId"),
            task.get("session_id"),
            session_id,
        ),
        "cursor_remote_task_id": _first_text(task.get("id")),
        "cursor_composer_id": "",
    }


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
    name = str(data.get("agentStatus") or data.get("status") or "").strip().lower()
    if name in STOPPED_STATUSES:
        return False
    # Cursor may keep UI chrome after completion; idle is not in-flight work.
    if name in IDLE_STATUSES or name in {"", "unknown"}:
        return False
    if name in BUSY_STATUSES:
        return True
    return bool(data.get("agentActivityLive"))


def composer_is_actively_working(result: Any) -> bool:
    """True only while Composer is in-flight, not idle leftover from a previous job."""
    if not isinstance(result, dict):
        return False
    if result.get("done") is True:
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in IDLE_STATUSES or status in {
        "not_started",
        "workspace_unavailable",
        "cursor_unavailable",
        "leftover_idle",
        "no_active_run",
    }:
        return False
    if status in BUSY_STATUSES:
        return True
    last = result.get("last")
    if last is not None and cursor_is_explicitly_busy(last):
        return True
    return bool(result.get("seen_busy") or result.get("started"))


def prompt_visible_in_composer(state: Any, prompt: str) -> bool:
    """True when Composer chat already contains this assignment's prompt."""
    data = parse_mcp_payload(state)
    parts: list[str] = []
    if isinstance(data, dict):
        try:
            parts.append(json.dumps(data, ensure_ascii=False, default=str))
        except TypeError:
            parts.append(str(data))
        for item in list(data.get("messages") or []):
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
    elif data is not None:
        parts.append(str(data))
    haystack = "\n".join(parts).lower()
    if not haystack.strip():
        return False
    needles: list[str] = []
    for line in (prompt or "").splitlines():
        text = " ".join(line.strip().split())
        if len(text) >= 20:
            needles.append(text[:96].lower())
        if len(needles) >= 4:
            break
    if not needles and (prompt or "").strip():
        needles.append((prompt or "").strip()[:64].lower())
    return any(needle in haystack for needle in needles)


_IN_FLIGHT_ASSISTANT = (
    "searching",
    "exploring",
    "reading",
    "looking through",
    "running tool",
    "i'll search",
    "let me search",
    "ищу ",
    "читаю ",
    "смотрю код",
    "поиск",
)


def cursor_has_active_work(state: Any) -> bool:
    """True if the chat shows in-flight search/tools — not leftover idle prose."""
    data = parse_mcp_payload(state)
    if not isinstance(data, dict):
        return False
    if data.get("pendingApprovals"):
        return True
    for item in list(data.get("messages") or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        if kind == "plan":
            completed = item.get("todosCompleted")
            total = item.get("todosTotal")
            try:
                if total is not None and int(total) > 0 and int(completed or 0) >= int(total):
                    continue
            except (TypeError, ValueError):
                pass
            return True
        if kind in {"tool", "tool_call", "thinking", "search"}:
            return True
        if kind == "human":
            continue
        if kind == "assistant":
            # Assistant prose is not a machine signal; word markers only in legacy mode.
            if _TEXT_HEURISTICS_ENABLED:
                text = str(item.get("text") or "").casefold()
                if any(marker in text for marker in _IN_FLIGHT_ASSISTANT):
                    return True
            continue
        if str(item.get("text") or "").strip():
            return True
    return False


def should_pin_cursor_followup(message: str) -> bool:
    """DEPRECATED keyword check; pin by the case's cursor_in_flight flag instead."""
    text = (message or "").lower()
    if not text:
        return True
    if not _TEXT_HEURISTICS_ENABLED:
        return False
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


def pin_cursor_followup_message(message: str, *, cursor_in_flight: bool | None = None) -> str:
    """Replace a follow-up with the check-only message when the case waits on Cursor.

    The machine signal is the work item's ``cursor_in_flight`` flag. The keyword scan
    remains only while legacy text heuristics are enabled.
    """
    if cursor_in_flight:
        return CURSOR_CHECK_ONLY_MESSAGE
    if cursor_in_flight is None and should_pin_cursor_followup(message):
        return CURSOR_CHECK_ONLY_MESSAGE
    if cursor_in_flight is False and _TEXT_HEURISTICS_ENABLED and should_pin_cursor_followup(message):
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
    assistant: list[str] = []
    plans: list[str] = []
    for item in list(state.get("messages") or [])[-8:]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "assistant":
            text = str(item.get("text") or "").strip()
            if text:
                assistant.append(text[:12000])
        elif kind == "plan":
            label = str(item.get("label") or "plan")
            desc = str(item.get("description") or "").strip()
            todos = f"{item.get('todosCompleted') or 0}/{item.get('todosTotal') or 0}"
            plans.append(f"[{label} {todos}] {desc}"[:1500])
    # Completion summary first; leftover plan UI may still be in the transcript.
    chunks = assistant[-2:] + plans[-1:]
    return "\n---\n".join(chunks)


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


async def _snapshot(
    session: Any,
    *,
    remote_task_id: str | None = None,
) -> tuple[Any, Any, str]:
    status: Any = None
    state: Any = None
    worker_result = ""
    worker_summary = ""
    try:
        status = await _poll_cursor_status(session, remote_task_id=remote_task_id)
    except Exception as exc:
        logger.info("CursorRemote get_status failed: %s", exc)
    if isinstance(status, dict):
        worker_result = str(status.get("result") or "").strip()
        worker_summary = str(status.get("summary") or "").strip()
    try:
        if remote_task_id:
            state = await mcp_call(
                session,
                "get_messages",
                {"taskId": remote_task_id, "task_id": remote_task_id, "messageLimit": 8},
            )
        else:
            state = await mcp_call(session, "get_state", {"messageLimit": 8})
    except Exception as exc:
        logger.info("CursorRemote get_state failed: %s", exc)
    summary = worker_result or summarize_cursor_state(state) or worker_summary
    return status, state, summary


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
    baseline_summary: str = "",
    work_item_id: Any = None,
    remote_task_id: str | None = None,
) -> dict[str, Any]:
    """Poll Cursor until it has actually worked and then gone idle.

    Immediate idle is not treated as completion: Cursor often looks idle right after
    send_prompt, before thinking starts. Long coding jobs should return done=false
    so the employee schedules a follow-up instead of telling the customer it is ready.
    """
    approvals: list[dict[str, Any]] = []
    seen_busy = False
    logged_busy = False
    last: Any = None
    deadline = time.monotonic() + max(1, timeout_ms) / 1000
    start = time.monotonic()
    log_cursor_stage(
        "wait_begin",
        work_item_id=work_item_id,
        timeout_ms=timeout_ms,
        require_busy=require_busy,
        start_grace_ms=start_grace_ms,
    )

    while time.monotonic() < deadline:
        if remote_task_id:
            try:
                await _poll_cursor_status(session, remote_task_id=remote_task_id)
            except Exception:
                pass
        approvals.extend(await click_pending_approvals(session))
        try:
            last = await _poll_cursor_status(session, remote_task_id=remote_task_id)
        except Exception as exc:
            last = {"error": str(exc)}
            await asyncio.sleep(2)
            continue

        if cursor_is_busy(last):
            seen_busy = True
            if not logged_busy:
                logged_busy = True
                log_cursor_stage(
                    "wait_busy",
                    work_item_id=work_item_id,
                    **status_snapshot(last),
                )
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
                if remote_task_id:
                    peek_state = await mcp_call(
                        session,
                        "get_messages",
                        {
                            "taskId": remote_task_id,
                            "task_id": remote_task_id,
                            "messageLimit": 8,
                        },
                    )
                else:
                    peek_state = await mcp_call(session, "get_state", {"messageLimit": 8})
            except Exception:
                peek_state = None
            if cursor_has_active_work(peek_state) and cursor_is_busy(last):
                seen_busy = True
                await asyncio.sleep(2)
                continue
            if (time.monotonic() - start) * 1000 < start_grace_ms:
                await asyncio.sleep(2)
                continue
            status, state, summary = await _snapshot(session, remote_task_id=remote_task_id)
            log_cursor_stage(
                "wait_not_started",
                work_item_id=work_item_id,
                **status_snapshot(status or last),
            )
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
            confirm = await _poll_cursor_status(session, remote_task_id=remote_task_id)
        except Exception:
            confirm = last
        if cursor_is_busy(confirm):
            seen_busy = True
            last = confirm
            continue

        status, state, summary = await _snapshot(session, remote_task_id=remote_task_id)
        if cursor_is_busy(status):
            seen_busy = True
            last = status
            continue
        if (
            baseline_summary.strip()
            and summary.strip() == baseline_summary.strip()
        ) or summary_looks_incomplete(summary):
            log_cursor_stage(
                "wait_awaiting_result",
                work_item_id=work_item_id,
                seen_busy=seen_busy,
                summary_chars=len(summary or ""),
                incomplete=summary_looks_incomplete(summary),
            )
            return _result(
                done=False,
                status="awaiting_result",
                last=status or confirm,
                state=state,
                summary=summary,
                approvals=approvals,
                seen_busy=seen_busy,
                hint=FOLLOW_UP_HINT,
            )
        log_cursor_stage(
            "wait_done",
            work_item_id=work_item_id,
            seen_busy=seen_busy,
            summary_chars=len(summary or ""),
        )
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

    status, state, summary = await _snapshot(session, remote_task_id=remote_task_id)
    name = "working" if cursor_is_busy(status or last) else _status_name(status or last)
    if name in {"idle", "unknown"} and seen_busy:
        name = "timeout"
    log_cursor_stage(
        "wait_timeout",
        work_item_id=work_item_id,
        status=name,
        seen_busy=seen_busy,
        **status_snapshot(status or last),
    )
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


async def peek_composer(
    session: Any,
    *,
    work_item_id: Any = None,
    remote_task_id: str | None = None,
) -> dict[str, Any]:
    """Cheap get_status probe: do not wait, do not treat leftover idle as busy."""
    try:
        status = await _poll_cursor_status(session, remote_task_id=remote_task_id)
    except Exception as exc:
        log_cursor_stage("peek_failed", work_item_id=work_item_id, error=str(exc)[:500])
        return {"ok": False, "busy": False, "error": str(exc)[:500], "last": None}
    snap = status_snapshot(status)
    busy = cursor_is_explicitly_busy(status)
    log_cursor_stage("peek", work_item_id=work_item_id, busy=busy, **snap)
    return {"ok": True, "busy": busy, "last": status, **snap}


async def wait_for_prompt_to_land(
    session: Any,
    *,
    prompt: str,
    baseline_summary: str = "",
    grace_ms: int = 12_000,
    work_item_id: Any = None,
    remote_task_id: str | None = None,
) -> dict[str, Any]:
    """After send_prompt: confirm Composer actually took this assignment."""
    deadline = time.monotonic() + max(50, int(grace_ms)) / 1000
    status: Any = None
    state: Any = None
    summary = ""
    ticks = 0
    while True:
        ticks += 1
        try:
            status, state, summary = await _snapshot(session, remote_task_id=remote_task_id)
        except Exception as exc:
            log_cursor_stage(
                "verify_failed",
                work_item_id=work_item_id,
                tick=ticks,
                error=str(exc)[:500],
            )
            status, state, summary = status, None, ""
        busy = cursor_is_explicitly_busy(status)
        visible = prompt_visible_in_composer(state, prompt)
        changed = bool(baseline_summary.strip()) and bool(summary.strip()) and (
            summary.strip() != baseline_summary.strip()
        )
        landed = busy or visible or changed
        log_cursor_stage(
            "verify",
            work_item_id=work_item_id,
            tick=ticks,
            landed=landed,
            busy=busy,
            visible=visible,
            summary_changed=changed,
            **status_snapshot(status),
            summary_chars=len(summary or ""),
        )
        if landed:
            return {
                "landed": True,
                "busy": busy,
                "visible": visible,
                "summary_changed": changed,
                "status": status,
                "state": state,
                "summary": summary,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "landed": False,
                "busy": False,
                "visible": visible,
                "summary_changed": changed,
                "status": status,
                "state": state,
                "summary": summary,
            }
        await asyncio.sleep(min(1.5, remaining))


async def _ensure_worker_session(
    session: Any,
    *,
    session_id: str | None,
    expected_workspace: str | None,
    expected_window_id: str | None,
    work_item_id: Any = None,
) -> dict[str, str]:
    sid = _first_text(session_id)
    if sid:
        return {"cursor_session_id": sid, "cursor_composer_id": ""}
    created = await mcp_call(
        session,
        "create_session",
        _omit_empty(
            {
                "workspaceId": expected_window_id,
                "workspace_id": expected_window_id,
                "workspacePath": expected_workspace,
                "workspace_path": expected_workspace,
            }
        ),
    )
    ids = _worker_ids_from_create(created)
    log_cursor_stage(
        "create_session",
        work_item_id=work_item_id,
        session_id=ids.get("cursor_session_id"),
        composer_id=ids.get("cursor_composer_id"),
        mcp=str(created)[:400],
    )
    if not ids.get("cursor_session_id"):
        raise RuntimeError(f"create_session did not return a session id: {created}")
    return ids


async def _send_worker_task(
    session: Any,
    prompt: str,
    *,
    timeout_ms: int,
    work_item_id: Any,
    expected_workspace: str | None,
    expected_window_id: str | None,
    task_id: str | None,
    chat_id: str | None,
    project_id: str | None,
    session_id: str | None,
    attachments: list[dict[str, Any]] | None,
    delivery: dict[str, Any],
    workspace_info: dict[str, Any],
    public_base_url: str = "",
    secret_key: str = "",
) -> dict[str, Any]:
    ids = await _ensure_worker_session(
        session,
        session_id=session_id,
        expected_workspace=expected_workspace,
        expected_window_id=expected_window_id,
        work_item_id=work_item_id,
    )
    from .cursor_callback import (
        CALLBACK_ARG_KEYS,
        callback_args_for_send_task,
        cursor_callback_url,
        unknown_callback_argument,
    )

    sid = ids["cursor_session_id"]
    send_payload = _omit_empty(
        {
            "sessionId": sid,
            "session_id": sid,
            "prompt": prompt,
            "projectId": project_id,
            "project_id": project_id,
            "taskId": str(task_id or work_item_id or ""),
            "task_id": str(task_id or work_item_id or ""),
            "chatId": chat_id,
            "chat_id": chat_id,
        }
    )
    callback_url = cursor_callback_url(
        work_item_id or task_id,
        public_base_url=public_base_url,
        secret_key=secret_key,
    )
    send_payload.update(callback_args_for_send_task(callback_url))
    if attachments:
        send_payload["attachments"] = attachments
    log_cursor_stage(
        "send_task",
        work_item_id=work_item_id,
        session_id=sid,
        task_id=send_payload.get("task_id"),
        chat_id=chat_id,
        project_id=project_id,
        prompt_chars=len(prompt),
        callback=bool(callback_url),
    )
    try:
        sent = await mcp_call(session, "send_task", send_payload)
    except Exception as exc:
        if callback_url and unknown_callback_argument(exc):
            stripped = {
                key: value
                for key, value in send_payload.items()
                if key not in CALLBACK_ARG_KEYS
            }
            log_cursor_stage(
                "send_task_retry_without_callback",
                work_item_id=work_item_id,
                error=str(exc)[:400],
            )
            sent = await mcp_call(session, "send_task", stripped)
            send_payload = stripped
            callback_url = ""
        elif session_id and "not found" in str(exc).lower():
            ids = await _ensure_worker_session(
                session,
                session_id=None,
                expected_workspace=expected_workspace,
                expected_window_id=expected_window_id,
                work_item_id=work_item_id,
            )
            sid = ids["cursor_session_id"]
            send_payload["sessionId"] = sid
            send_payload["session_id"] = sid
            sent = await mcp_call(session, "send_task", send_payload)
        else:
            log_cursor_stage(
                "send_task_failed",
                work_item_id=work_item_id,
                error=str(exc)[:800],
            )
            return {
                "ok": False,
                "done": False,
                "sent": False,
                "prompt_sent": False,
                "started": False,
                "seen_busy": False,
                "status": "cursor_unavailable",
                "reason": f"send_task failed: {exc}",
                "summary": f"send_task failed: {exc}",
                "next": CURSOR_UNAVAILABLE_HINT,
                "workspace": workspace_info.get("workspace"),
                "windows": workspace_info.get("windows") or [],
            }
    task_ids = _worker_ids_from_task(sent, session_id=sid)
    remote_id = task_ids.get("cursor_remote_task_id") or ""
    composer_id = ids.get("cursor_composer_id") or task_ids.get("cursor_composer_id") or ""
    window_id = ids.get("cursor_window_id") or expected_window_id or ""
    workspace_path = (
        ids.get("cursor_workspace")
        or expected_workspace
        or workspace_info.get("workspace")
        or ""
    )
    log_cursor_stage(
        "send_task_ok",
        work_item_id=work_item_id,
        session_id=sid,
        remote_task_id=remote_id,
        composer_id=composer_id or None,
        window_id=window_id or None,
        mcp=str(sent)[:500],
    )
    landed = await wait_for_prompt_to_land(
        session,
        prompt=prompt,
        grace_ms=min(12_000, max(int(timeout_ms), 50)),
        work_item_id=work_item_id,
        remote_task_id=remote_id or None,
    )
    if not landed.get("landed") and remote_id:
        try:
            worker_status = await _poll_cursor_status(
                session, remote_task_id=remote_id
            )
        except Exception:
            worker_status = None
        if cursor_is_explicitly_busy(worker_status):
            landed = {**landed, "landed": True, "busy": True, "status": worker_status}
        else:
            # send_task already created a worker task. The composer window often
            # stays idle for a while, which is not "the prompt was rejected".
            log_cursor_stage(
                "send_task_accepted_waiting",
                work_item_id=work_item_id,
                session_id=sid,
                remote_task_id=remote_id,
            )
            return {
                "ok": True,
                "done": False,
                "sent": sent,
                "prompt_sent": True,
                "started": True,
                "seen_busy": False,
                "status": "awaiting_result",
                "reason": "",
                "summary": landed.get("summary") or "",
                "next": FOLLOW_UP_HINT,
                "workspace": workspace_path or workspace_info.get("workspace"),
                "windows": workspace_info.get("windows") or [],
                "last": worker_status or landed.get("status"),
                "cursor_session_id": sid,
                "cursor_remote_task_id": remote_id,
                "cursor_composer_id": composer_id,
                "cursor_window_id": window_id,
                "cursor_workspace": workspace_path,
                "workspace": workspace_path,
                "task_id": str(task_id or work_item_id or ""),
                "chat_id": chat_id,
                "file_delivery": {
                    "method": delivery.get("method"),
                    "paths": delivery.get("paths") or [],
                },
            }
    if not landed.get("landed"):
        reason = (
            "send_task returned, but Composer did not start this assignment."
        )
        return {
            "ok": False,
            "done": False,
            "sent": sent,
            "prompt_sent": False,
            "started": False,
            "seen_busy": False,
            "status": "not_started",
            "reason": reason,
            "summary": landed.get("summary") or "",
            "next": NOT_STARTED_HINT,
            "workspace": workspace_info.get("workspace"),
            "windows": workspace_info.get("windows") or [],
            "last": landed.get("status"),
            "cursor_session_id": sid,
            "cursor_remote_task_id": remote_id,
            "cursor_composer_id": composer_id,
            "cursor_window_id": window_id,
            "cursor_workspace": workspace_path,
            "workspace": workspace_path,
            "file_delivery": {
                "method": delivery.get("method"),
                "paths": delivery.get("paths") or [],
            },
        }
    driven = await drive_until_done(
        session,
        timeout_ms=timeout_ms,
        require_busy=True,
        work_item_id=work_item_id,
        remote_task_id=remote_id or None,
    )
    result = {
        **driven,
        "sent": sent,
        "prompt_sent": True,
        "started": bool(
            driven.get("seen_busy") or landed.get("busy") or driven.get("started")
        ),
        "seen_busy": bool(driven.get("seen_busy") or landed.get("busy")),
        "prompt_visible": bool(landed.get("visible")),
        "cursor_session_id": sid,
        "cursor_remote_task_id": remote_id,
        "cursor_composer_id": composer_id,
        "cursor_window_id": window_id,
        "cursor_workspace": workspace_path,
        "workspace": workspace_path,
        "task_id": str(task_id or work_item_id or ""),
        "chat_id": chat_id,
        "file_delivery": {
            "method": delivery.get("method"),
            "paths": delivery.get("paths") or [],
        },
    }
    if str(result.get("status") or "") == "not_started":
        result["status"] = "working" if result.get("seen_busy") else "awaiting_result"
        result["ok"] = True
        result["done"] = False
    if delivery.get("paths"):
        result["images"] = delivery["paths"]
    log_cursor_stage(
        "waiting" if not result.get("done") else "finished",
        work_item_id=work_item_id,
        status=result.get("status"),
        done=bool(result.get("done")),
        session_id=sid,
        remote_task_id=remote_id,
        summary_chars=len(str(result.get("summary") or "")),
    )
    return result


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
    task_id: str | None = None,
    chat_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    remote_task_id: str | None = None,
) -> dict[str, Any]:
    log_cursor_stage(
        "send_begin",
        work_item_id=work_item_id,
        prompt_chars=len(text or ""),
        expected_workspace=expected_workspace,
        expected_window_id=expected_window_id,
        task_id=task_id,
        chat_id=chat_id,
        session_id=session_id,
        remote_task_id=remote_task_id,
    )
    ensure = await ensure_cursor_workspace(
        session,
        expected_workspace=expected_workspace,
        expected_window_id=expected_window_id,
    )
    log_cursor_stage(
        "workspace",
        work_item_id=work_item_id,
        ok=bool(ensure.get("ok")),
        status=ensure.get("status"),
        workspace=ensure.get("workspace"),
        windows=ensure.get("windows") or [],
        reason=ensure.get("reason"),
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
    tool_names = await list_mcp_tool_names(session)
    use_worker = worker_tools_available(tool_names)
    try:
        current = await mcp_call(session, "get_status")
    except Exception as exc:
        current = ensure.get("last")
        log_cursor_stage(
            "status_before_failed",
            work_item_id=work_item_id,
            error=str(exc)[:500],
        )
    before = status_snapshot(current)
    log_cursor_stage("status_before", work_item_id=work_item_id, use_worker=use_worker, **before)
    if not use_worker and cursor_is_explicitly_busy(current):
        log_cursor_stage(
            "skip_busy",
            work_item_id=work_item_id,
            reason="composer_explicitly_busy",
            **before,
        )
        return {
            "ok": False,
            "done": False,
            "sent": False,
            "prompt_sent": False,
            "skipped_prompt": True,
            "started": False,
            "seen_busy": True,
            "status": "cursor_busy",
            "reason": (
                "Composer is actually working. Did not send this assignment."
            ),
            "summary": "",
            "next": FOLLOW_UP_HINT,
            "workspace": ensure.get("workspace"),
            "windows": ensure.get("windows") or [],
            "last": current,
        }
    from .cursor_file_transfer import build_customer_files_prompt, deliver_customer_files_to_cursor

    delivery = await deliver_customer_files_to_cursor(
        session,
        attachments,
        work_item_id=work_item_id,
        public_base_url=public_base_url,
        secret_key=secret_key,
    )
    log_cursor_stage(
        "files",
        work_item_id=work_item_id,
        method=delivery.get("method"),
        paths=delivery.get("paths") or [],
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
    if use_worker:
        return await _send_worker_task(
            session,
            prompt,
            timeout_ms=timeout_ms,
            work_item_id=work_item_id,
            expected_workspace=expected_workspace,
            expected_window_id=expected_window_id,
            task_id=task_id,
            chat_id=chat_id,
            project_id=project_id,
            session_id=session_id,
            attachments=inline_attachments,
            delivery=delivery,
            workspace_info=ensure,
            public_base_url=public_base_url,
            secret_key=secret_key,
        )
    try:
        _, _, baseline_summary = await _snapshot(session)
    except Exception as exc:
        baseline_summary = ""
        log_cursor_stage(
            "baseline_failed",
            work_item_id=work_item_id,
            error=str(exc)[:500],
        )
    log_cursor_stage(
        "send_prompt",
        work_item_id=work_item_id,
        prompt_chars=len(prompt),
        baseline_chars=len(baseline_summary or ""),
    )
    try:
        sent = await mcp_call(session, "send_prompt", send_payload)
    except Exception as exc:
        log_cursor_stage(
            "send_prompt_failed",
            work_item_id=work_item_id,
            error=str(exc)[:800],
        )
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
    log_cursor_stage(
        "send_prompt_ok",
        work_item_id=work_item_id,
        mcp=str(sent)[:500],
    )
    try:
        after_send = await mcp_call(session, "get_status")
    except Exception as exc:
        after_send = None
        log_cursor_stage(
            "status_after_failed",
            work_item_id=work_item_id,
            error=str(exc)[:500],
        )
    else:
        log_cursor_stage(
            "status_after",
            work_item_id=work_item_id,
            **status_snapshot(after_send),
        )
    landed = await wait_for_prompt_to_land(
        session,
        prompt=prompt,
        baseline_summary=baseline_summary,
        grace_ms=min(12_000, max(int(timeout_ms), 50)),
        work_item_id=work_item_id,
    )
    if not landed.get("landed"):
        reason = (
            "send_prompt returned, but Composer did not start this assignment. "
            "The chat is still idle with the previous contents — prompt did not land."
        )
        log_cursor_stage(
            "not_delivered",
            work_item_id=work_item_id,
            reason=reason,
            visible=landed.get("visible"),
            busy=landed.get("busy"),
            summary_changed=landed.get("summary_changed"),
            **status_snapshot(landed.get("status")),
        )
        return {
            "ok": False,
            "done": False,
            "sent": sent,
            "prompt_sent": False,
            "started": False,
            "seen_busy": False,
            "status": "not_started",
            "reason": reason,
            "summary": landed.get("summary") or "",
            "next": NOT_STARTED_HINT,
            "workspace": ensure.get("workspace"),
            "windows": ensure.get("windows") or [],
            "last": landed.get("status") or after_send,
            "messages": (
                (landed.get("state") or {}).get("messages")
                if isinstance(landed.get("state"), dict)
                else []
            ),
            "file_delivery": {
                "method": delivery.get("method"),
                "paths": delivery.get("paths") or [],
            },
        }
    log_cursor_stage(
        "delivered",
        work_item_id=work_item_id,
        busy=landed.get("busy"),
        visible=landed.get("visible"),
        summary_changed=landed.get("summary_changed"),
        **status_snapshot(landed.get("status")),
    )
    driven = await drive_until_done(
        session,
        timeout_ms=timeout_ms,
        require_busy=True,
        baseline_summary=baseline_summary,
        work_item_id=work_item_id,
    )
    result = {
        **driven,
        "sent": sent,
        "prompt_sent": True,
        "started": True,
        "seen_busy": bool(driven.get("seen_busy") or landed.get("busy")),
        "prompt_visible": bool(landed.get("visible")),
        "baseline_summary": baseline_summary,
        "file_delivery": {
            "method": delivery.get("method"),
            "paths": delivery.get("paths") or [],
        },
    }
    if str(result.get("status") or "") == "not_started":
        result["status"] = "working" if result.get("seen_busy") else "awaiting_result"
        result["ok"] = True
        result["done"] = False
    if delivery.get("paths"):
        result["images"] = delivery["paths"]
    log_cursor_stage(
        "waiting" if not result.get("done") else "finished",
        work_item_id=work_item_id,
        status=result.get("status"),
        done=bool(result.get("done")),
        seen_busy=bool(result.get("seen_busy")),
        summary_chars=len(str(result.get("summary") or "")),
    )
    return result


async def check_and_drive(
    session: Any,
    *,
    timeout_ms: int = 90_000,
    idle_debounce_ms: int = 3_000,
    baseline_summary: str = "",
    work_item_id: Any = None,
    remote_task_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    chat_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Poll an already-running Cursor job. Idle without prior activity is a real finish here."""
    del session_id, task_id, chat_id, project_id
    log_cursor_stage(
        "poll_begin",
        work_item_id=work_item_id,
        timeout_ms=timeout_ms,
        remote_task_id=remote_task_id,
    )
    result = await drive_until_done(
        session,
        timeout_ms=timeout_ms,
        require_busy=False,
        start_grace_ms=0,
        idle_debounce_ms=idle_debounce_ms,
        baseline_summary=baseline_summary,
        work_item_id=work_item_id,
        remote_task_id=remote_task_id,
    )
    if remote_task_id:
        result["cursor_remote_task_id"] = remote_task_id
    if baseline_summary:
        result["baseline_summary"] = baseline_summary
    log_cursor_stage(
        "poll_end",
        work_item_id=work_item_id,
        status=result.get("status"),
        done=result.get("done"),
        seen_busy=result.get("seen_busy"),
        summary_chars=len(str(result.get("summary") or "")),
    )
    return result
