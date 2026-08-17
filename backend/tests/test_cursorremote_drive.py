from app.cursorremote_drive import _approval_actions, parse_mcp_payload


def test_parse_mcp_json_text() -> None:
    payload = parse_mcp_payload([{"type": "text", "text": '{"pendingApprovalCount": 1}'}])
    assert payload["pendingApprovalCount"] == 1


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
