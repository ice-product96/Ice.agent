from pathlib import Path

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cursor_callback import (
    apply_cursor_callback,
    cursor_callback_token,
    cursor_callback_url,
    cursor_task_complete_url,
    normalize_cursor_callback_payload,
    resolve_work_item_for_task_callback,
    unknown_callback_argument,
    verify_cursor_bearer_secret,
    verify_cursor_callback_token,
    verify_cursor_webhook_signature,
)
from app.db import Agent, Base, CursorRun, Customer, WorkItem
from app.pm_state import get_or_create_cursor_run


async def sessions_for(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def make_item(db, **overrides) -> WorkItem:
    agent = Agent(name=f"pm-{overrides.get('project_id', 'default')}")
    db.add(agent)
    await db.flush()
    values = {
        "agent_id": agent.id,
        "title": "Persist PM state",
        "goal": "Store deterministic PM state",
        "project_id": "ice",
        "task_type": "technical",
        "requirements": ["Save phase and execution state."],
        "acceptance_criteria": ["State survives a new database session."],
    }
    values.update(overrides)
    item = WorkItem(**values)
    db.add(item)
    await db.flush()
    return item


def test_callback_token_roundtrip() -> None:
    token = cursor_callback_token(46, "secret")
    assert verify_cursor_callback_token(46, token, "secret")
    assert not verify_cursor_callback_token(46, token, "wrong")
    assert not verify_cursor_callback_token(46, "", "secret")


def test_callback_url_includes_signed_token() -> None:
    url = cursor_callback_url(
        46,
        public_base_url="https://agent.example/",
        secret_key="secret",
    )
    token = cursor_callback_token(46, "secret")
    assert url == f"https://agent.example/api/v1/cursor/callback/46?token={token}"
    assert cursor_callback_url(46, public_base_url="", secret_key="secret") == ""


def test_task_complete_url() -> None:
    assert (
        cursor_task_complete_url(public_base_url="http://192.168.10.64:8040/")
        == "http://192.168.10.64:8040/api/v1/cursor/task-complete"
    )
    assert cursor_task_complete_url(public_base_url="") == ""


def test_bearer_secret_roundtrip() -> None:
    assert verify_cursor_bearer_secret("shared", "Bearer shared")
    assert verify_cursor_bearer_secret("shared", "bearer shared")
    assert not verify_cursor_bearer_secret("shared", "Bearer other")
    assert not verify_cursor_bearer_secret("shared", "shared")
    assert not verify_cursor_bearer_secret("", "Bearer shared")


def test_webhook_signature_accepts_sha256_prefix() -> None:
    body = b'{"event":"statusChange","status":"FINISHED"}'
    hex_digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_cursor_webhook_signature("secret", body, "sha256=" + hex_digest)
    assert verify_cursor_webhook_signature("secret", body, hex_digest)
    assert not verify_cursor_webhook_signature("secret", body, "sha256=deadbeef")
    assert not verify_cursor_webhook_signature("secret", body, "")


def test_normalize_cloud_finished_summary() -> None:
    payload = normalize_cursor_callback_payload(
        {
            "event": "statusChange",
            "status": "FINISHED",
            "id": "bc-1",
            "summary": "Warehouses screen added. Open /warehouses to verify.",
        }
    )
    assert payload["done"] is True
    assert payload["callback"] is True
    assert payload["status"] == "idle"
    assert "Warehouses screen" in payload["summary"]
    assert payload["cursor_remote_task_id"] == "bc-1"


def test_normalize_task_completed_event() -> None:
    payload = normalize_cursor_callback_payload(
        {
            "event": "task_completed",
            "done": True,
            "summary": "Каталог в шапке.",
            "prompt": "добавь каталог",
            "files": ["src/header.css"],
            "workspacePath": "d:/projects/mysell",
            "workspaceName": "mysell",
            "windowId": "win-1",
            "composerId": "composer-9",
        }
    )
    assert payload["done"] is True
    assert payload["status"] == "idle"
    assert payload["summary"] == "Каталог в шапке."
    assert payload["cursor_composer_id"] == "composer-9"
    assert payload["cursor_window_id"] == "win-1"
    assert payload["workspace"].endswith("mysell")


def test_normalize_failed_is_terminal() -> None:
    payload = normalize_cursor_callback_payload(
        {"event": "statusChange", "status": "ERROR", "summary": "Agent crashed while applying patch"}
    )
    assert payload["done"] is True
    assert payload["status"] == "error"
    assert payload["result"]["status"] == "failed"


def test_unknown_callback_argument_detects_schema_rejection() -> None:
    assert unknown_callback_argument(
        RuntimeError("additional properties 'callbackUrl' not allowed")
    )
    assert not unknown_callback_argument(RuntimeError("unknown tool send_task"))
    assert not unknown_callback_argument(RuntimeError("session not found"))


def test_cursor_callback_rejects_bad_token(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cursor/callback/1?token=nope",
        json={"event": "statusChange", "status": "FINISHED", "summary": "done"},
    )
    assert response.status_code == 403


def test_cursor_callback_404_with_valid_token(client: TestClient) -> None:
    token = cursor_callback_token(999999, "test-secret")
    response = client.post(
        f"/api/v1/cursor/callback/999999?token={token}",
        json={
            "event": "statusChange",
            "status": "FINISHED",
            "summary": "Something was implemented and can be verified.",
        },
    )
    assert response.status_code == 404


def test_task_complete_rejects_missing_bearer(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cursor/task-complete",
        json={
            "event": "task_completed",
            "done": True,
            "summary": "Done.",
            "workspacePath": "d:/projects/mysell",
        },
    )
    assert response.status_code == 403


def test_task_complete_404_when_no_inflight(client: TestClient) -> None:
    response = client.post(
        "/api/v1/cursor/task-complete",
        headers={"Authorization": "Bearer test-secret"},
        json={
            "event": "task_completed",
            "done": True,
            "summary": "Done.",
            "workspacePath": "d:/projects/nobody",
            "composerId": "missing",
        },
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resolve_task_callback_by_composer(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "resolve-composer.db")
    async with sessions() as db:
        item = await make_item(
            db,
            pm_phase="IN_DEVELOPMENT",
            status="waiting_external",
            metadata_json={
                "cursor_in_flight": True,
                "cursor_composer_id": "composer-abc",
            },
        )
        other = await make_item(
            db,
            project_id="other",
            pm_phase="IN_DEVELOPMENT",
            status="waiting_external",
            metadata_json={"cursor_composer_id": "composer-zzz"},
        )
        await db.commit()
        matched = await resolve_work_item_for_task_callback(
            db,
            {
                "event": "task_completed",
                "composerId": "composer-abc",
                "workspacePath": "d:/projects/ice.agent",
            },
        )
        assert matched is not None
        assert matched.id == item.id
        assert matched.id != other.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_task_callback_by_customer_workspace(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "resolve-ws.db")
    async with sessions() as db:
        customer = Customer(
            id="mysell",
            name="MySell",
            project_id="mysell",
            cursor_workspace="d:/projects/mysell",
            cursor_window_id="win-mysell",
        )
        db.add(customer)
        await db.flush()
        item = await make_item(
            db,
            project_id="mysell",
            customer_id=customer.id,
            pm_phase="IN_DEVELOPMENT",
            status="waiting_external",
            metadata_json={"cursor_in_flight": True},
        )
        await db.commit()
        matched = await resolve_work_item_for_task_callback(
            db,
            {
                "event": "task_completed",
                "workspacePath": "D:\\projects\\mysell",
                "windowId": "win-mysell",
            },
        )
        assert matched is not None
        assert matched.id == item.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_cursor_callback_uses_summary_and_moves_to_qa(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "callback-qa.db")
    async with sessions() as db:
        item = await make_item(
            db,
            pm_phase="READY_FOR_DEV",
            status="in_progress",
            acceptance_criteria=["Можно создать склад"],
            metadata_json={"cursor_in_flight": False},
        )
        run, _created = await get_or_create_cursor_run(
            db, item, attempt=1, request={"brief": "warehouses"}
        )
        run.status = "running"
        await db.commit()

        result = await apply_cursor_callback(
            db,
            item,
            normalize_cursor_callback_payload(
                {
                    "event": "statusChange",
                    "status": "FINISHED",
                    "id": "remote-46",
                    "summary": (
                        "Warehouses screen added. Open /warehouses and save a stock count."
                    ),
                }
            ),
        )
        stored = await db.get(WorkItem, item.id)
        assert result["status"] == "completed"
        assert result["qa_required"] is True
        assert stored is not None
        assert stored.pm_phase == "QA"
        assert stored.metadata_json.get("cursor_remote_task_id") == "remote-46"
        active = await db.get(CursorRun, stored.active_cursor_run_id)
        assert active is not None
        assert active.status == "completed"
        assert active.result_json["native_summary"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_cursor_callback_skips_already_finished(tmp_path: Path) -> None:
    engine, sessions = await sessions_for(tmp_path / "callback-skip.db")
    async with sessions() as db:
        item = await make_item(db, pm_phase="QA", status="in_progress")
        await db.commit()
        result = await apply_cursor_callback(
            db,
            item,
            normalize_cursor_callback_payload(
                {
                    "event": "statusChange",
                    "status": "FINISHED",
                    "summary": "Warehouses screen added. Open /warehouses to verify.",
                }
            ),
        )
        assert result["skipped"] is True
        assert result["reason"] == "already_finished"
    await engine.dispose()
