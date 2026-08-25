import asyncio
import pytest
from fastapi.testclient import TestClient
from api.app import app
from core.database import init_db


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    asyncio.run(init_db())


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_root_and_health(client):
    r1 = client.get("/")
    assert r1.status_code == 200
    assert "text/html" in r1.headers.get("content-type", "")
    assert "OrchisX Engine" in r1.text
    r2 = client.get("/health")
    assert r2.status_code == 200
    assert r2.json()["status"] == "ok"

    r3 = client.get("/api/v1/health")
    assert r3.status_code == 200
    data = r3.json()
    assert "status" in data
    assert "active_proxies" in data
    assert "active_accounts" in data


def test_openapi_schema(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["title"] == "OrchisX Engine API"
    assert "/api/v1/x/tweets/search" in schema["paths"]
    assert "/api/v1/x/users/{username}" in schema["paths"]
    assert "/api/v1/extractions" in schema["paths"]
    assert "/api/v1/monitors" in schema["paths"]
    assert "/api/v1/accounts" in schema["paths"]


def test_account_crud(client):
    # Add account
    r_post = client.post("/api/v1/accounts", json={
        "auth_token": "api_test_auth_token_999",
        "ct0": "api_test_ct0_999",
        "username": "api_tester"
    })
    assert r_post.status_code == 200
    acc = r_post.json()
    assert acc["username"] == "api_tester"
    assert acc["status"] == "active"
    acc_id = acc["id"]

    # List accounts
    r_list = client.get("/api/v1/accounts")
    assert r_list.status_code == 200
    accounts = r_list.json()
    assert any(a["id"] == acc_id for a in accounts)

    # Delete account
    r_del = client.delete(f"/api/v1/accounts/{acc_id}")
    assert r_del.status_code == 200
    assert r_del.json()["success"] is True


def test_proxies_list(client):
    resp = client.get("/api/v1/proxies")
    assert resp.status_code == 200
    proxies = resp.json()
    assert isinstance(proxies, list)
    assert len(proxies) >= 10


def test_monitors_crud(client):
    # Create monitor
    r_post = client.post("/api/v1/monitors", json={
        "name": "AI Tech Monitor",
        "query": "artificial intelligence",
        "monitor_type": "search",
        "interval_seconds": 60,
        "webhook_url": "https://example.com/webhook"
    })
    assert r_post.status_code == 200
    mon = r_post.json()
    assert mon["name"] == "AI Tech Monitor"
    assert mon["status"] == "active"
    assert mon["webhook_secret"] is not None
    mon_id = mon["id"]

    # List monitors
    r_list = client.get("/api/v1/monitors")
    assert r_list.status_code == 200
    monitors = r_list.json()
    assert any(m["id"] == mon_id for m in monitors)

    # Delete monitor
    r_del = client.delete(f"/api/v1/monitors/{mon_id}")
    assert r_del.status_code == 200
    assert r_del.json()["success"] is True


def test_extraction_creation_and_polling(client):
    r_post = client.post("/api/v1/extractions", json={
        "query": "python programming",
        "results_limit": 5,
        "tool_type": "search",
        "format": "json"
    })
    assert r_post.status_code == 200
    job = r_post.json()
    assert job["query"] == "python programming"
    assert job["results_limit"] == 5
    assert job["format"] == "json"
    job_id = job["id"]

    # Poll status
    r_get = client.get(f"/api/v1/extractions/{job_id}")
    assert r_get.status_code == 200
    assert r_get.json()["id"] == job_id
