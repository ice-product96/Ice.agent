"""Drive CursorRemote: send work and click Allow/Accept without asking a human."""

from __future__ import annotations

import json
import logging
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


def parse_mcp_payload(content: Any) -> Any:
    if isinstance(content, dict) and "text" in content and len(content) <= 3:
        text = content.get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    if isinstance(content, list):
        if len(content) == 1:
            return parse_mcp_payload(content[0])
        return [parse_mcp_payload(item) for item in content]
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


async def mcp_call(session: Any, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    response = await session.call_tool(tool, dict(arguments or {}))
    content = [item.model_dump() for item in response.content]
    if getattr(response, "isError", False):
        detail = "; ".join(str(item.get("text") or item) for item in content)
        raise RuntimeError(detail or f"MCP tool {tool} failed")
    return parse_mcp_payload(content)


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


async def drive_until_idle(
    session: Any,
    *,
    timeout_ms: int = 120_000,
    max_rounds: int = 12,
) -> dict[str, Any]:
    """Wait for Cursor to go idle, auto-approving Allow/Accept along the way."""
    approvals: list[dict[str, Any]] = []
    last: Any = None
    for _ in range(max(1, max_rounds)):
        approvals.extend(await click_pending_approvals(session))
        try:
            last = await mcp_call(
                session,
                "wait",
                {"for": "needs_input", "timeoutMs": min(15_000, timeout_ms)},
            )
        except Exception:
            last = None
        pending = 0
        if isinstance(last, dict):
            pending = int(last.get("pendingApprovalCount") or 0)
            if last.get("status") == "needs_input" and pending:
                continue
        try:
            idle = await mcp_call(
                session,
                "wait",
                {"for": "idle", "timeoutMs": min(20_000, timeout_ms)},
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "approvals": approvals, "last": last}
        if isinstance(idle, dict) and idle.get("status") == "idle":
            return {"ok": True, "status": "idle", "approvals": approvals, "last": idle}
        last = idle
        if isinstance(idle, dict) and int(idle.get("pendingApprovalCount") or 0) > 0:
            continue
        break
    return {"ok": True, "status": "stopped", "approvals": approvals, "last": last}


async def send_prompt_and_drive(session: Any, text: str, *, timeout_ms: int = 180_000) -> dict[str, Any]:
    sent = await mcp_call(session, "send_prompt", {"text": text})
    driven = await drive_until_idle(session, timeout_ms=timeout_ms)
    return {"sent": sent, **driven}
