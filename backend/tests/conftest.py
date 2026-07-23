import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(__file__).parent / "test.db"
DB_PATH.unlink(missing_ok=True)
os.environ["ICE_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
os.environ["ICE_ADMIN_PASSWORD"] = "test-password"
os.environ["ICE_SECRET_KEY"] = "test-secret"
os.environ["ICE_TASK_WORKERS"] = "0"

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def token(client: TestClient) -> str:
    response = client.post("/api/auth/login", json={"password": "test-password"})
    assert response.status_code == 200
    return response.json()["token"]


@pytest.fixture()
def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
