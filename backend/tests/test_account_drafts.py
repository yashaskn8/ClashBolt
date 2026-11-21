"""Named-account drafts must remain private and version-fenced."""

import os

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.api import create_app
from fastapi.testclient import TestClient


def test_account_drafts_do_not_leak_between_operators(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database required")
    store = AccountStore(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute("TRUNCATE account_login_throttle, local_account CASCADE")
    for name, role in [
        ("alice.op", "operator"),
        ("bob.op", "operator"),
        ("read.only", "viewer"),
    ]:
        store.create_account(name, role, "correct-long-passphrase-123")

    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "account-drafts-ci-signing-key-123456789")
    def login(client, name):
        response = client.post(
            "/api/auth/login",
            json={"username": name, "password": "correct-long-passphrase-123"},
        )
        assert response.status_code == 200, response.text

    def draft(variant):
        return {
            "expected_version": 0,
            "variant": variant,
            "schedule": "serial",
            "execution_backend": "memory",
        }

    with TestClient(create_app()) as client:
        login(client, "alice.op")
        assert client.get("/api/experiment-draft").json()["saved"] is None
        alice = client.post("/api/experiment-draft", json=draft("racy"))
        assert alice.status_code == 200, alice.text
        assert alice.json()["version"] == 1
        assert client.post("/api/experiment-draft", json=draft("locked")).status_code == 409

        login(client, "bob.op")
        assert client.get("/api/experiment-draft").json()["saved"] is None
        bob = client.post("/api/experiment-draft", json=draft("locked"))
        assert bob.status_code == 200
        assert bob.json()["version"] == 1
        assert bob.json()["draft"]["variant"] == "locked"

        login(client, "alice.op")
        own = client.get("/api/experiment-draft").json()["saved"]
        assert own["version"] == 1
        assert own["draft"]["variant"] == "racy"
        forbidden = client.post(
            "/api/experiment-draft",
            json={**draft("locked"), "account_id": "not-allowed"},
        )
        assert forbidden.status_code == 422

        login(client, "read.only")
        assert client.get("/api/experiment-draft").json()["saved"] is None
        assert client.post("/api/experiment-draft", json=draft("locked")).status_code == 403
