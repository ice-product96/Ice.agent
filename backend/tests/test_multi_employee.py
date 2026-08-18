from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Agent, Consultation, TelegramAccount, WorkItem
from app.routing import TelegramEventRouter

from test_lifecycle import RecordingEvents, RoutingRuntime, RoutingTelegram, sessions_for


@pytest.mark.asyncio
async def test_telegram_router_picks_agent_by_open_work_item(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "multi-agent.db")
    async with sessions() as db:
        account = TelegramAccount(
            phone="+100000",
            name="shared",
            session_path="shared.session",
            authorized=True,
        )
        db.add(account)
        await db.flush()
        agent_a = Agent(name="agent-a", telegram_account_id=account.id)
        agent_b = Agent(name="agent-b", telegram_account_id=account.id)
        db.add_all([agent_a, agent_b])
        await db.flush()
        db.add(
            WorkItem(
                agent_id=agent_b.id,
                title="client case",
                status="open",
                chat_id="20",
                reply_phone="+100000",
            )
        )
        await db.commit()
        agent_a_id, agent_b_id = agent_a.id, agent_b.id

    runtime = RoutingRuntime()
    telegram = RoutingTelegram()
    events = RecordingEvents()
    router = TelegramEventRouter(sessions, runtime, telegram, events)
    payload = {
        "phone": "+100000",
        "sender_id": 10,
        "chat_id": 20,
        "message_id": 30,
        "text": "hello",
        "outgoing": False,
        "service": False,
        "is_admin": False,
    }
    await router.new_message(payload)
    assert len(runtime.calls) == 1
    assert runtime.calls[0][0] == agent_b_id
    assert runtime.calls[0][0] != agent_a_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_pick_agent_by_consultation_id(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "multi-consult.db")
    async with sessions() as db:
        account = TelegramAccount(
            phone="+100000",
            name="shared",
            session_path="shared.session",
            authorized=True,
        )
        db.add(account)
        await db.flush()
        agent_a = Agent(name="agent-a", telegram_account_id=account.id)
        agent_b = Agent(name="agent-b", telegram_account_id=account.id)
        db.add_all([agent_a, agent_b])
        await db.flush()
        consult = Consultation(
            agent_id=agent_b.id,
            question="Need approval",
            status="open",
        )
        db.add(consult)
        await db.commit()
        consult_id = consult.id

    router = TelegramEventRouter(sessions, RoutingRuntime(), RoutingTelegram(), RecordingEvents())
    async with sessions() as db:
        account, agents = await router._account_and_agents(db, "+100000")
        assert account is not None
        picked = await router._pick_agent(
            db,
            account,
            agents,
            chat_id="99",
            consult_id=consult_id,
        )
        assert picked is not None
        assert picked.id == agent_b.id
    await engine.dispose()


def test_employees_roster_lists_all_agents(client: TestClient, headers: dict[str, str]) -> None:
    first = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "employee-one", "prompt": "one"},
    ).json()
    second = client.post(
        "/api/agents",
        headers=headers,
        json={"name": "employee-two", "prompt": "two"},
    ).json()

    roster = client.get("/api/v1/employees", headers=headers)
    assert roster.status_code == 200
    body = roster.json()
    ids = {item["agent_id"] for item in body["items"]}
    assert first["id"] in ids
    assert second["id"] in ids
    one = next(item for item in body["items"] if item["agent_id"] == first["id"])
    two = next(item for item in body["items"] if item["agent_id"] == second["id"])
    assert one["agent_name"] == "employee-one"
    assert two["agent_name"] == "employee-two"
    assert "work_item_counts" in one
    assert "actionable_work_items" in one


def test_create_cron_scopes_name_by_agent(client: TestClient, headers: dict[str, str]) -> None:
    first = client.post("/api/agents", headers=headers, json={"name": "cron-a", "prompt": "a"}).json()
    second = client.post("/api/agents", headers=headers, json={"name": "cron-b", "prompt": "b"}).json()
    payload = {
        "name": "daily-check",
        "agent_id": first["id"],
        "schedule": "0 9 * * *",
        "prompt": "check inbox",
        "enabled": True,
    }
    job_a = client.post("/api/v1/cron", headers=headers, json=payload)
    assert job_a.status_code == 201
    payload["agent_id"] = second["id"]
    job_b = client.post("/api/v1/cron", headers=headers, json=payload)
    assert job_b.status_code == 201
    assert job_a.json()["name"] != job_b.json()["name"]
    assert job_a.json()["name"].startswith(f"a{first['id']}-")
    assert job_b.json()["name"].startswith(f"a{second['id']}-")
