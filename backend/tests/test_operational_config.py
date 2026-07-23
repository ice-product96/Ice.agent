import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from app.config import get_settings
from app.secrets import SecretStore

from conftest import DB_PATH


def v1_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-password"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def profile(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    key: str,
    base_url: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/llm-profiles",
        headers=headers,
        json={
            "name": name,
            "provider": "custom-openai-compatible",
            "base_url": base_url,
            "api_key": key,
            "default_model": f"{name}-model",
            "enabled": True,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_profile_encryption_masking_and_delete_protection(
    client: TestClient,
) -> None:
    headers = v1_headers(client)
    created = profile(
        client, headers, "encrypted-profile", "plain-secret-value", "https://one.test/v1"
    )
    assert created["has_api_key"] is True
    assert created["api_key_masked"] == "********"
    assert "api_key" not in created
    loaded = client.get(
        f"/api/v1/llm-profiles/{created['id']}", headers=headers
    ).json()
    assert "plain-secret-value" not in str(loaded)

    with sqlite3.connect(DB_PATH) as connection:
        ciphertext = connection.execute(
            "select api_key_ciphertext from llm_profiles where id = ?",
            (created["id"],),
        ).fetchone()[0]
    assert "plain-secret-value" not in ciphertext
    assert (
        SecretStore.from_settings(get_settings()).decrypt(ciphertext)
        == "plain-secret-value"
    )

    preserved = client.patch(
        f"/api/v1/llm-profiles/{created['id']}",
        headers=headers,
        json={"api_key": "", "name": "encrypted-profile"},
    )
    assert preserved.json()["has_api_key"] is True

    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "protected-agent", "llm_profile_id": created["id"]},
    ).json()
    blocked = client.delete(
        f"/api/v1/llm-profiles/{created['id']}", headers=headers
    )
    assert blocked.status_code == 409
    client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert (
        client.delete(
            f"/api/v1/llm-profiles/{created['id']}", headers=headers
        ).status_code
        == 204
    )


def test_two_agents_resolve_independent_profiles(
    client: TestClient,
    monkeypatch: Any,
) -> None:
    headers = v1_headers(client)
    first = profile(client, headers, "profile-one", "key-one", "https://one.test/v1")
    second = profile(client, headers, "profile-two", "key-two", "https://two.test/v1")
    agents = [
        client.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": f"independent-{item['id']}", "llm_profile_id": item["id"]},
        ).json()
        for item in (first, second)
    ]
    resolved: list[tuple[str, str | None, str]] = []

    class FakeLLM:
        def __init__(
            self,
            *,
            api_key: str,
            base_url: str | None,
            model: str,
            max_rounds: int,
        ) -> None:
            resolved.append((api_key, base_url, model))

        async def complete(self, *args: Any, **kwargs: Any) -> str:
            return "ok"

    monkeypatch.setattr("app.runtime.LLMClient", FakeLLM)
    for agent in agents:
        response = client.post(
            f"/api/agents/{agent['id']}/run",
            headers=headers,
            json={"message": "test"},
        )
        assert response.status_code == 200
    assert resolved == [
        ("key-one", "https://one.test/v1", "profile-one-model"),
        ("key-two", "https://two.test/v1", "profile-two-model"),
    ]


def test_runtime_settings_and_telegram_credentials_are_masked(
    client: TestClient,
) -> None:
    headers = v1_headers(client)
    saved = client.put(
        "/api/v1/settings/runtime",
        headers=headers,
        json={
            "search_provider": "ddg",
            "memory_enabled": False,
            "memory_backend": "platform",
            "mem0_api_key": "mem0-plain-key",
            "typing_min_seconds": 0.2,
            "typing_max_seconds": 0.8,
            "typing_jitter_seconds": 0.1,
            "typing_chunk_size": 1000,
            "typing_presence": True,
            "task_workers": 0,
            "max_tool_rounds": 4,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["has_mem0_api_key"] is True
    assert "mem0-plain-key" not in str(saved.json())
    assert client.get("/api/v1/settings/runtime", headers=headers).json()[
        "max_tool_rounds"
    ] == 4

    seen: dict[str, Any] = {}
    original = client.app.state.telegram

    async def request_code(account: Any) -> str:
        seen["api_id"] = account.api_id
        seen["hash"] = SecretStore.from_settings(get_settings()).decrypt(
            account.api_hash_ciphertext
        )
        return "code-hash"

    original_request = original.request_code
    original.request_code = request_code
    try:
        response = client.post(
            "/api/v1/telegram/accounts/login",
            headers=headers,
            json={
                "name": "account-specific",
                "phone": "+1999000111",
                "api_id": 12345,
                "api_hash": "telegram-plain-hash",
            },
        )
    finally:
        original.request_code = original_request
    assert response.status_code == 200
    assert seen == {"api_id": 12345, "hash": "telegram-plain-hash"}
    accounts = client.get("/api/v1/telegram/accounts", headers=headers).json()
    account = next(item for item in accounts if item["phone"] == "+1999000111")
    assert account["has_api_hash"] is True
    assert "telegram-plain-hash" not in str(account)
