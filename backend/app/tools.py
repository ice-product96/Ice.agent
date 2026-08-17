import asyncio
import inspect
import json
import random
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin


class DangerousActionError(PermissionError):
    pass


# Granted automatically when the agent has the `telegram` tool enabled.
TELEGRAM_OPERATIONAL_PERMISSIONS = {
    "telegram_send_message",
    "telegram_send_file",
    "telegram_edit_message",
    "telegram_forward_messages",
    "telegram_join_channel",
    "telegram_send_reaction",
    "telegram_acknowledge_read",
    "telegram_save_draft",
    "telegram_escalate",
}

# Granted automatically when the agent has the `sip` tool enabled.
SIP_OPERATIONAL_PERMISSIONS = {
    "sip_dial",
    "sip_hangup",
    "sip_status",
}

# Granted when autonomy/employee mode is enabled on the agent profile or tools include employee.
EMPLOYEE_OPERATIONAL_PERMISSIONS = {
    "plan_upsert",
    "plan_get",
    "plan_complete_step",
    "need_upsert",
    "need_satisfy",
    "consult_manager",
    "request_approval",
    "self_configure",
    "employee_status",
    "schedule_self",
    "schedule_self_list",
    "schedule_self_cancel",
}

# Granted when agent has tool_permissions "cursorremote" or MCP server `cursorremote` attached.
CURSORREMOTE_OPERATIONAL_PERMISSIONS = {
    "mcp_cursorremote_tools",
    "mcp_cursorremote_run",
    "mcp_cursorremote_send_prompt",
    "mcp_cursorremote_approve",
    "mcp_cursorremote_reject",
    "mcp_cursorremote_approve_all",
    "mcp_cursorremote_click_action",
    "mcp_cursorremote_new_chat",
    "mcp_cursorremote_switch_tab",
    "mcp_cursorremote_switch_window",
    "mcp_cursorremote_set_mode",
    "mcp_cursorremote_set_model",
    "mcp_cursorremote_set_plan_model",
}

# Dangerous tools that require a fresh manager approval while autonomy is on.
APPROVAL_REQUIRED_TOOLS = {
    "telegram_delete_messages",
    "telegram_delete_dialog",
    "telegram_leave_channel",
    "sip_dial",
    "mcp_cursorremote_send_prompt",
    "mcp_cursorremote_approve",
    "mcp_cursorremote_click_action",
    "mcp_cursorremote_new_chat",
    "mcp_cursorremote_switch_window",
}


def resolve_tool_permissions(
    agent_config: dict[str, Any] | None,
    *,
    employee_autonomy: bool = False,
    cursorremote_attached: bool = False,
) -> set[str]:
    config = agent_config or {}
    permissions = {str(item) for item in (config.get("tool_permissions") or [])}
    tools = {str(item) for item in (config.get("tools") or [])}
    if "telegram" in tools:
        permissions |= TELEGRAM_OPERATIONAL_PERMISSIONS
    if "sip" in tools:
        permissions |= SIP_OPERATIONAL_PERMISSIONS
    if employee_autonomy or "employee" in tools or "autonomy" in tools:
        permissions |= EMPLOYEE_OPERATIONAL_PERMISSIONS
    if cursorremote_attached or "cursorremote" in permissions or "cursorremote" in tools:
        permissions |= CURSORREMOTE_OPERATIONAL_PERMISSIONS
    return permissions


MAX_LLM_TOOLS = 128


def effective_tool_name(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Map meta/gateway calls to the underlying tool for policy and approval checks."""
    name = str(tool_name or "").strip()
    args = arguments or {}
    if name == "tools_run":
        inner = str(args.get("tool_name") or args.get("name") or "").strip()
        if inner:
            return inner
    if name.startswith("mcp_") and name.endswith("_run"):
        inner = str(args.get("tool") or "").strip()
        if inner:
            return f"{name[:-4]}_{inner}"
    return name


class ToolPolicy:
    dangerous_names = {
        "delete", "remove", "shell", "exec", "payment", "purchase", "transfer",
        "send_message", "send_file", "forward_message", "edit_message", "join_channel",
        "leave_channel", "reaction", "draft", "escalate", "ban", "kick",
        "change_permissions", "schedule_self", "sip_dial", "sip_hangup",
        "request_approval", "self_configure",
        # CursorRemote MCP mutating tools (matched as substrings of mcp_cursorremote_*)
        "cursorremote_send_prompt", "cursorremote_approve", "cursorremote_reject",
        "cursorremote_approve_all", "cursorremote_click_action", "cursorremote_new_chat",
        "cursorremote_switch_tab", "cursorremote_switch_window",
        "cursorremote_set_mode", "cursorremote_set_model", "cursorremote_set_plan_model",
    }

    def is_dangerous(self, tool_name: str) -> bool:
        normalized = tool_name.lower()
        return any(part in normalized for part in self.dangerous_names)

    def check(self, tool_name: str, arguments: dict[str, Any], allowed: set[str] | None = None) -> None:
        effective = effective_tool_name(tool_name, arguments)
        normalized = effective.lower()
        dangerous = any(part in normalized for part in self.dangerous_names)
        if dangerous and (allowed is None or effective not in allowed):
            raise DangerousActionError(f"Tool '{effective}' requires explicit permission")
        if any(key in arguments for key in ("password", "secret", "token")) and "use_secrets" not in (allowed or set()):
            raise DangerousActionError("Passing secrets to tools requires explicit permission")


def _json_type(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    if origin is list:
        return {"type": "array", "items": _json_type(get_args(annotation)[0])}
    if origin is dict:
        return {"type": "object"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    return {"type": "string"}


@dataclass(slots=True)
class RegisteredTool:
    name: str
    function: Callable[..., Any]
    description: str
    parameters: dict[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        if self.parameters is not None:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": self.parameters,
                },
            }
        signature = inspect.signature(self.function)
        properties = {
            name: {**_json_type(parameter.annotation), "description": name}
            for name, parameter in signature.parameters.items()
        }
        required = [name for name, parameter in signature.parameters.items() if parameter.default is inspect.Parameter.empty]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }


class ToolRegistry:
    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self.tools: dict[str, RegisteredTool] = {}
        self.audit: list[dict[str, Any]] = []
        self.before_call: Callable[[str, dict[str, Any]], Any] | None = None
        self.llm_permissions: set[str] | None = None

    def register(
        self,
        function: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        tool_name = name or function.__name__
        self.tools[tool_name] = RegisteredTool(
            tool_name,
            function,
            description or inspect.getdoc(function) or tool_name,
            parameters,
        )

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def call(self, name: str, arguments: dict[str, Any], permissions: set[str] | None = None) -> Any:
        record: dict[str, Any] = {"tool": name, "arguments": arguments, "status": "running"}
        self.audit.append(record)
        try:
            self.policy.check(name, arguments, permissions)
            if self.before_call is not None:
                maybe = self.before_call(name, arguments)
                if inspect.isawaitable(maybe):
                    await maybe
            tool = self.tools.get(name)
            if tool is None:
                raise KeyError(f"Unknown tool: {name}")
            result = tool.function(**arguments)
            result = await result if inspect.isawaitable(result) else result
            record.update(status="success", result=result)
            return result
        except BaseException as exc:
            record.update(status="error", error=str(exc))
            raise


async def sleep(seconds: float) -> str:
    """Wait for a bounded number of seconds."""
    await asyncio.sleep(min(max(seconds, 0), 10))
    return "ok"


def parse_json(value: str) -> Any:
    """Parse a JSON string."""
    return json.loads(value)


def common_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(sleep)
    registry.register(parse_json)
    return registry


async def human_delay(text: str, minimum: float = 0.4, maximum: float = 2.5) -> None:
    await asyncio.sleep(min(maximum, max(minimum, len(text) * random.uniform(0.015, 0.04))))
