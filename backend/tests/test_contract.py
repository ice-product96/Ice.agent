from fastapi.testclient import TestClient


def v1_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-password"},
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_v1_login_me_dashboard_and_agent_crud(client: TestClient) -> None:
    headers = v1_headers(client)
    assert client.get("/api/v1/auth/me", headers=headers).json()["is_admin"] is True

    target = client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "contract-target", "description": "Target", "model": "gpt-5.5", "provider": "openai"},
    )
    assert target.status_code == 201

    created = client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "contract-source",
            "description": "Frontend-shaped agent",
            "prompt": "Help",
            "model": "deepseek-chat",
            "provider": "deepseek",
            "tools": ["web_search"],
            "links": [str(target.json()["id"])],
            "typing_enabled": False,
            "status": "active",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["description"] == "Frontend-shaped agent"
    assert body["model"] == "deepseek-chat"
    assert body["links"][0]["target_agent_id"] == target.json()["id"]

    updated = client.put(
        f"/api/v1/agents/{body['id']}",
        headers=headers,
        json={**body, "description": "Updated", "links": []},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "Updated"
    assert updated.json()["links"] == []
    patched = client.patch(
        f"/api/v1/agents/{body['id']}",
        headers=headers,
        json={"typing_enabled": True},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "contract-source"
    assert patched.json()["typing_enabled"] is True

    dashboard = client.get("/api/v1/dashboard", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["agents_count"] >= 2

    assert client.delete(f"/api/v1/agents/{body['id']}", headers=headers).status_code == 204
    assert client.delete(f"/api/v1/agents/{target.json()['id']}", headers=headers).status_code == 204


def test_v1_admin_settings_round_trip(client: TestClient) -> None:
    headers = v1_headers(client)
    saved = client.put(
        "/api/v1/settings/admin",
        headers=headers,
        json={
            "admin_ids": ["123", 456],
            "escalation_enabled": True,
            "escalation_chat_id": "-1001",
            "escalation_prompt": "Escalate this",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["admin_ids"] == ["123", "456"]

    loaded = client.get("/api/v1/settings/admin", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["escalation_enabled"] is True
    assert loaded.json()["escalation_chat_id"] == "-1001"
