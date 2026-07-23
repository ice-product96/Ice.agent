import asyncio
import inspect
import json
import random
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin


class DangerousActionError(PermissionError):
    pass


class ToolPolicy:
    dangerous_names = {
        "delete", "remove", "shell", "exec", "payment", "purchase", "transfer",
        "send_message", "send_file", "forward_message", "edit_message", "join_channel",
        "leave_channel", "reaction", "draft", "escalate", "ban", "kick",
        "change_permissions",
    }

    def check(self, tool_name: str, arguments: dict[str, Any], allowed: set[str] | None = None) -> None:
        normalized = tool_name.lower()
        dangerous = any(part in normalized for part in self.dangerous_names)
        if dangerous and (allowed is None or tool_name not in allowed):
            raise DangerousActionError(f"Tool '{tool_name}' requires explicit permission")
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
        self.policy.check(name, arguments, permissions)
        tool = self.tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        result = tool.function(**arguments)
        return await result if inspect.isawaitable(result) else result


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
