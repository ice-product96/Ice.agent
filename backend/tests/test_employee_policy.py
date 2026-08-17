from types import SimpleNamespace

from app.employee_policy import (
    action_matches_tool,
    approval_required_for_tool,
    employee_policy,
    normalize_action_name,
)


def test_normalize_action_name_maps_human_text_to_tool() -> None:
    assert normalize_action_name("Исходящий звонок Валерию") == "sip_dial"
    assert normalize_action_name("sip_dial") == "sip_dial"


def test_action_matches_tool_accepts_aliases() -> None:
    assert action_matches_tool("Исходящий звонок Валерию", "sip_dial")
    assert action_matches_tool("sip_dial", "sip_dial")
    assert not action_matches_tool("telegram_delete_messages", "sip_dial")


def test_customer_chat_skips_sip_approval_by_default() -> None:
    profile = SimpleNamespace(config_json={})
    assert not approval_required_for_tool(
        profile,
        "sip_dial",
        {"source": "telegram", "is_admin": False},
    )


def test_sip_requires_approval_when_customer_bypass_disabled() -> None:
    profile = SimpleNamespace(config_json={
        "policy": {
            "approval_required_tools": ["sip_dial"],
            "customer_requests_without_approval": False,
        }
    })
    assert approval_required_for_tool(
        profile,
        "sip_dial",
        {"source": "telegram", "is_admin": False},
    )


def test_manager_chat_skips_approval_by_default() -> None:
    profile = SimpleNamespace(config_json={})
    assert not approval_required_for_tool(
        profile,
        "mcp_cursorremote_send_prompt",
        {"source": "telegram", "is_admin": True},
    )


def test_cursorremote_never_requires_manager_approval() -> None:
    profile = SimpleNamespace(config_json={
        "policy": {
            "approval_required_tools": ["mcp_cursorremote_send_prompt", "mcp_cursorremote_approve"],
            "customer_requests_without_approval": False,
            "manager_orders_without_approval": False,
        }
    })
    assert not approval_required_for_tool(
        profile,
        "mcp_cursorremote_approve",
        {"source": "telegram", "is_admin": False},
    )
    assert not approval_required_for_tool(
        profile,
        "cursorremote_do",
        {"source": "telegram", "is_admin": False},
    )
    policy = employee_policy(SimpleNamespace(config_json={"policy": {"consult_manager_on_idle_tick": True}}))
    assert policy["consult_manager_on_idle_tick"] is True
    assert "sip_dial" not in policy["approval_required_tools"]
