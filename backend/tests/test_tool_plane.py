from app.tool_plane import (
    attach_tool_plane,
    build_catalog,
    is_surface_tool,
    schemas_for_tool_plane,
    search_catalog,
)
from app.tools import ToolRegistry, effective_tool_name


def test_surface_vs_catalog_split() -> None:
    registry = ToolRegistry()
    registry.register(lambda: None, "telegram_send_message", "Send")
    registry.register(lambda: None, "telegram_delete_messages", "Delete")
    attach_tool_plane(registry)
    schemas = schemas_for_tool_plane(registry, {"telegram_send_message", "telegram_delete_messages"})
    names = {item["function"]["name"] for item in schemas}
    assert "telegram_send_message" in names
    assert "telegram_delete_messages" not in names
    assert "tools_search" in names
    assert "tools_run" in names


def test_effective_tool_name_tools_run() -> None:
    assert effective_tool_name("tools_run", {"tool_name": "sip_dial"}) == "sip_dial"


def test_mcp_namespace_detection() -> None:
    from app.tool_plane import tool_namespace

    assert tool_namespace("mcp_ice_tracker_run") == "mcp:ice"
    assert tool_namespace("memory_search") == "memory"


def test_build_catalog_marks_surface() -> None:
    registry = ToolRegistry()
    registry.register(lambda: None, "web_search", "Search web")
    registry.register(lambda: None, "telegram_join_channel", "Join")
    catalog = build_catalog(registry)
    by_name = {entry.name: entry for entry in catalog}
    assert by_name["web_search"].surface is True
    assert by_name["telegram_join_channel"].surface is False


def test_plan_tools_are_not_surface() -> None:
    assert is_surface_tool("plan_get") is False
    assert is_surface_tool("plan_upsert") is False
    assert is_surface_tool("schedule_self") is True
    assert is_surface_tool("cursorremote_check") is True


def test_search_catalog_by_namespace() -> None:
    registry = ToolRegistry()
    registry.register(lambda: None, "sip_dial", "Call")
    registry.register(lambda: None, "telegram_send_message", "Send")
    hits = search_catalog(build_catalog(registry), "", namespace="sip", limit=5)
    assert len(hits) == 1
    assert hits[0]["name"] == "sip_dial"
