import asyncio
import json
from pathlib import Path

from app.cursorremote_drive import (
    _approval_actions,
    check_and_drive,
    cursor_has_active_work,
    cursor_is_busy,
    drive_until_done,
    parse_mcp_payload,
    pin_cursor_followup_message,
    send_prompt_and_drive,
    summarize_cursor_state,
)


class _Item:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def model_dump(self) -> dict:
        if isinstance(self.payload, str):
            return {"type": "text", "text": self.payload, "annotations": None}
        return {
            "type": "text",
            "text": json.dumps(self.payload),
            "annotations": None,
            "_meta": {},
        }


class _Resp:
    def __init__(self, payload: object, is_error: bool = False) -> None:
        self.content = [_Item(payload)]
        self.isError = is_error


class ScriptSession:
    def __init__(
        self,
        queues: dict[str, list],
        default: dict | None = None,
        *,
        tool_names: list[str] | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.queues = {key: list(values) for key, values in queues.items()}
        self.calls: list[tuple[str, dict]] = []
        self.default = default or {
            "agentStatus": "idle",
            "pendingApprovalCount": 0,
            "agentActivityLive": False,
        }
        self.tool_names = list(tool_names or [])
        self.workspace = workspace

    async def list_tools(self):
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name

        class _Result:
            tools = [_Tool(name) for name in self.tool_names]

        return _Result()

    async def call_tool(self, tool: str, arguments: dict | None = None):
        self.calls.append((tool, dict(arguments or {})))
        if tool == "write_workspace_file" and self.workspace is not None:
            rel = str((arguments or {}).get("relativePath") or (arguments or {}).get("path") or "")
            raw_b64 = (arguments or {}).get("content_base64") or ""
            target = self.workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            import base64

            target.write_bytes(base64.b64decode(raw_b64))
            return _Resp({"ok": True, "path": rel})
        queue = self.queues.setdefault(tool, [])
        if not queue:
            return _Resp(self.default)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)


def test_parse_mcp_json_text() -> None:
    payload = parse_mcp_payload([{"type": "text", "text": '{"pendingApprovalCount": 1}'}])
    assert payload["pendingApprovalCount"] == 1


def test_parse_mcp_json_text_with_extra_keys() -> None:
    payload = parse_mcp_payload(
        {
            "type": "text",
            "text": '{"agentStatus": "waiting_approval", "pendingApprovalCount": 1}',
            "annotations": None,
            "_meta": {},
        }
    )
    assert payload["pendingApprovalCount"] == 1
    assert cursor_is_busy(payload)


def test_approval_actions_skip_reject() -> None:
    pending = [
        {
            "id": "1",
            "actions": [
                {"type": "approve", "label": "Allow", "selectorPath": "#allow"},
                {"type": "reject", "label": "Skip", "selectorPath": "#skip"},
            ],
        }
    ]
    actions = _approval_actions(pending)
    assert len(actions) == 1
    assert actions[0]["selectorPath"] == "#allow"


def test_cursor_is_busy_statuses() -> None:
    assert cursor_is_busy({"agentStatus": "thinking"})
    assert cursor_is_busy({"agentStatus": "searching"})
    assert cursor_is_busy({"agentStatus": "exploring"})
    assert cursor_is_busy({"agentStatus": "idle", "agentActivityLive": True})
    assert cursor_is_busy({"agentStatus": "idle", "pendingApprovalCount": 2})
    assert cursor_is_busy(
        {
            "type": "text",
            "text": '{"agentStatus": "waiting_approval", "pendingApprovalCount": 1}',
            "annotations": None,
        }
    )
    assert not cursor_is_busy({"agentStatus": "idle", "pendingApprovalCount": 0})


def test_search_messages_count_as_active_work() -> None:
    assert cursor_has_active_work(
        {"messages": [{"type": "assistant", "text": "Searching the codebase…"}]}
    )
    assert cursor_has_active_work({"messages": [{"type": "plan", "label": "search"}]})
    assert not cursor_has_active_work({"messages": [{"type": "human", "text": "do LAVVE"}]})


def test_pin_followup_blocks_restart_wording() -> None:
    pinned = pin_cursor_followup_message("Cursor остановился на поиске, дай задачу заново")
    assert "cursorremote_check" in pinned
    assert "cursorremote_do" in pinned
    assert "не давай" in pinned.lower() or "Не вызывай" in pinned


def test_summarize_cursor_state_uses_assistant_text() -> None:
    summary = summarize_cursor_state(
        {
            "messages": [
                {"type": "human", "text": "do LAVVE"},
                {"type": "assistant", "text": "Workspace ready, changes applied."},
            ]
        }
    )
    assert "Workspace ready" in summary
    assert "do LAVVE" not in summary


def test_drive_does_not_treat_immediate_idle_as_done() -> None:
    session = ScriptSession({})
    result = asyncio.run(
        drive_until_done(session, timeout_ms=500, start_grace_ms=0, idle_debounce_ms=0, require_busy=True)
    )
    assert result["done"] is False
    assert result["status"] == "not_started"
    assert result["ok"] is True
    assert "next" in result


def test_drive_returns_done_after_busy_then_idle() -> None:
    session = ScriptSession(
        {
            "get_status": [
                {"agentStatus": "thinking", "pendingApprovalCount": 0},
                {"agentStatus": "thinking", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
            ],
            "wait": [{"status": "needs_input", "pendingApprovalCount": 0}],
            "get_state": [
                {
                    "pendingApprovals": [],
                    "messages": [{"type": "assistant", "text": "LAVVE changes applied."}],
                }
            ],
        }
    )
    result = asyncio.run(
        drive_until_done(session, timeout_ms=2000, start_grace_ms=0, idle_debounce_ms=0, require_busy=True)
    )
    assert result["done"] is True
    assert result["status"] == "idle"
    assert "LAVVE changes applied" in result["summary"]
    assert "Do NOT call schedule_self" in result["next"]


def test_drive_timeout_while_working() -> None:
    busy = {"agentStatus": "generating", "pendingApprovalCount": 0, "agentActivityLive": True}
    session = ScriptSession({"wait": [{"status": "timeout"} for _ in range(20)]}, default=busy)
    result = asyncio.run(
        drive_until_done(session, timeout_ms=80, start_grace_ms=0, idle_debounce_ms=0, require_busy=True)
    )
    assert result["done"] is False
    assert result["status"] in {"working", "generating", "timeout"}
    assert "schedule_self" in result["next"]


def test_check_idle_is_done() -> None:
    session = ScriptSession(
        {
            "get_status": [
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
            ],
            "get_state": [
                {"messages": [{"type": "assistant", "text": "Already finished."}]}
            ],
        }
    )
    result = asyncio.run(check_and_drive(session, timeout_ms=500, idle_debounce_ms=0))
    assert result["done"] is True
    assert "Already finished" in result["summary"]


def test_check_does_not_finish_on_waiting_approval() -> None:
    waiting = {
        "agentStatus": "waiting_approval",
        "pendingApprovalCount": 1,
        "agentActivityLive": False,
    }
    session = ScriptSession(
        {"wait": [{"status": "timeout"} for _ in range(20)]},
        default=waiting,
    )
    result = asyncio.run(check_and_drive(session, timeout_ms=80, idle_debounce_ms=0))
    assert result["done"] is False


def test_send_prompt_skipped_when_cursor_already_working() -> None:
    busy = {"agentStatus": "searching", "pendingApprovalCount": 0, "agentActivityLive": True}
    session = ScriptSession(
        {
            "get_status": [
                busy,
                busy,
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
                {"agentStatus": "idle", "pendingApprovalCount": 0},
            ],
            "wait": [{"status": "needs_input", "pendingApprovalCount": 0}],
            "get_state": [
                {"messages": [{"type": "assistant", "text": "Search finished, patch applied."}]}
            ],
        },
        default={"agentStatus": "idle", "pendingApprovalCount": 0},
    )
    result = asyncio.run(send_prompt_and_drive(session, "ты остановился на поиске, сделай ещё раз"))
    assert result["skipped_prompt"] is True
    assert result["prompt_sent"] is False
    assert not any(tool == "send_prompt" for tool, _ in session.calls)
    assert result["done"] is True


def test_searching_status_is_not_treated_as_not_started() -> None:
    searching = {"agentStatus": "searching", "pendingApprovalCount": 0, "agentActivityLive": False}
    session = ScriptSession({"wait": [{"status": "timeout"} for _ in range(20)]}, default=searching)
    result = asyncio.run(
        drive_until_done(session, timeout_ms=80, start_grace_ms=0, idle_debounce_ms=0, require_busy=True)
    )
    assert result["done"] is False
    assert result["status"] != "not_started"


def test_send_prompt_uploads_customer_image_via_mcp_write(tmp_path) -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    workspace = tmp_path / "site"
    workspace.mkdir()
    session = ScriptSession(
        {"wait": [{"status": "timeout"} for _ in range(20)]},
        default={
            "agentStatus": "idle",
            "pendingApprovalCount": 0,
            "workspacePath": str(workspace),
        },
        tool_names=["write_workspace_file"],
        workspace=workspace,
    )
    asyncio.run(
        send_prompt_and_drive(
            session,
            "Поставь это фото в шапку",
            attachments=[{"kind": "image", "filename": "hero.png", "mime_type": "image/png", "data_b64": png}],
            work_item_id=12,
            timeout_ms=80,
        )
    )
    sent = next(args for tool, args in session.calls if tool == "send_prompt")
    assert "from-customer/case-12" in sent["text"]
    write_calls = [args for tool, args in session.calls if tool == "write_workspace_file"]
    assert write_calls
    rel = write_calls[0]["relativePath"]
    assert (workspace / rel).is_file()


def test_workspace_match_normalizes_paths() -> None:
    from app.cursorremote_drive import workspace_matches, workspace_paths_from_status

    assert workspace_matches(
        r"d:\projects\uraltrade",
        [r"D:/projects/uraltrade"],
    )
    assert workspace_matches("uraltrade", [r"D:/projects/uraltrade"])
    assert not workspace_matches(
        r"d:\projects\uraltrade",
        [r"D:/projects/lavve"],
    )
    paths = workspace_paths_from_status(
        {
            "windows": [
                {"workspacePath": r"D:\projects\uraltrade", "id": "w1"},
                {"workspace": r"D:\projects\lavve"},
            ]
        }
    )
    assert any("uraltrade" in p for p in paths)


def test_send_prompt_refuses_when_workspace_closed() -> None:
    session = ScriptSession(
        {},
        default={
            "agentStatus": "idle",
            "pendingApprovalCount": 0,
            "workspacePath": r"D:\projects\other",
        },
    )
    result = asyncio.run(
        send_prompt_and_drive(
            session,
            "deploy",
            expected_workspace=r"d:/projects/uraltrade",
            timeout_ms=80,
        )
    )
    assert result["prompt_sent"] is False
    assert result["status"] == "workspace_unavailable"
    assert result["done"] is False
    assert not any(tool == "send_prompt" for tool, _ in session.calls)


def test_prompt_actually_started_requires_busy() -> None:
    from app.cursorremote_drive import prompt_actually_started

    assert not prompt_actually_started(
        {"prompt_sent": True, "status": "not_started", "seen_busy": False, "done": False}
    )
    assert prompt_actually_started(
        {"prompt_sent": True, "status": "working", "seen_busy": True, "done": False}
    )
    assert not prompt_actually_started(
        {"prompt_sent": False, "status": "workspace_unavailable", "done": False}
    )


