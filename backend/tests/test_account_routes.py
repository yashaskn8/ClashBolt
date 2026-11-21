import os
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import LOGIN_ATTEMPTS_PER_MINUTE, AccountStore
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from fastapi.testclient import TestClient


@pytest.fixture
def named_accounts(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable PostgreSQL test database is required")
    store = AccountStore(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute("TRUNCATE account_login_throttle, local_account CASCADE")
    for username, role in (
        ("sample.viewer", "viewer"),
        ("sample.operator", "operator"),
        ("sample.reviewer", "reviewer"),
    ):
        store.create_account(username, role, "sample-long-local-password-555")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "test-session-key-32-chars-long-1234567")
    monkeypatch.delenv("CLASHBOLT_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("CLASHBOLT_REVIEWER_TOKEN", raising=False)
    return store


def _login(client: TestClient, role: str):
    response = client.post(
        "/api/auth/login",
        json={
            "username": f"sample.{role}",
            "password": "sample-long-local-password-555",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == role
    assert "httponly" in response.headers["set-cookie"].lower()


def test_named_account_permissions_and_immutable_reviewers(named_accounts):
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/catalog").json()["access_mode"] == "accounts"
        assert client.get("/api/oracle/proposals").status_code == 401
        assert client.post("/api/oracle/proposals", json={}).status_code == 401

        _login(client, "viewer")
        assert client.get("/api/auth/me").json()["username"] == "sample.viewer"
        assert client.get("/api/oracle/proposals").status_code == 200
        assert client.post("/api/oracle/proposals", json={}).status_code == 403

        _login(client, "operator")
        created = client.post(
            "/api/oracle/proposals", json={"proposed_by": "sample.reviewer"}
        )
        assert created.status_code == 201, created.text
        proposal = created.json()
        with named_accounts.connect() as conn:
            author = conn.execute(
                "SELECT proposed_by FROM oracle_proposal WHERE id = %s",
                (UUID(proposal["id"]),),
            ).fetchone()["proposed_by"]
        assert author == "sample.operator"
        approve_url = f"/api/oracle/proposals/{proposal['id']}/approve"
        payload = {"expected_hash": ORACLE_HASH, "reviewer": "sample.operator"}
        assert client.post(approve_url, json=payload).status_code == 403

        _login(client, "reviewer")
        assert client.post("/api/oracle/proposals", json={}).status_code == 403
        approved = client.post(approve_url, json=payload)
        assert approved.status_code == 200, approved.text
        assert approved.json()["reviewed_by"] == "sample.reviewer"
        run = {
            "client_key": str(uuid4()),
            "variant": "locked",
            "schedule": "serial",
            "approved_oracle_id": proposal["id"],
        }
        assert client.post("/api/runs", json=run).status_code == 403

        _login(client, "operator")
        admitted = client.post("/api/runs", json=run)
        assert admitted.status_code == 201, admitted.text
        assert client.get(f"/api/runs/{admitted.json()['run_id']}").status_code == 200
        assert client.get("/api/tool-audit").status_code == 200
        assert client.post(approve_url, json=payload).status_code == 403

        assert named_accounts.disable_account("sample.operator")
        assert client.get("/api/runs").status_code == 401


def test_named_account_logout_and_bad_password(named_accounts):
    with TestClient(create_app()) as client:
        failed = client.post(
            "/api/auth/login",
            json={"username": "sample.operator", "password": "incorrect"},
        )
        assert failed.status_code == 401
        assert client.get("/api/auth/me").status_code == 401
        _login(client, "viewer")
        copied_cookie = client.cookies.get("clashbolt_identity")
        assert copied_cookie
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        client.cookies.set("clashbolt_identity", copied_cookie, path="/api")
        assert client.get("/api/auth/me").status_code == 401


def test_account_mode_requires_explicit_secrets(monkeypatch):
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="Account mode needs"):
        create_app()
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "postgresql://localhost/example")
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "short")
    with pytest.raises(ValueError, match="32-character"):
        create_app()


def test_account_readiness_requires_an_active_identity(named_accounts):
    with TestClient(create_app()) as client:
        assert client.get("/api/ready").status_code == 200
        for role in ("viewer", "operator", "reviewer"):
            assert named_accounts.disable_account(f"sample.{role}")
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert "account" in response.json()["detail"]


def test_named_account_operations_restricted_to_operator(named_accounts):
    with TestClient(create_app()) as client:
        _login(client, "viewer")
        assert client.get("/api/operations").status_code == 403
        _login(client, "reviewer")
        assert client.get("/api/operations").status_code == 403
        _login(client, "operator")
        response = client.get("/api/operations")
        assert response.status_code == 200
        assert "responsive_workers" in response.json()


def test_login_throttle_denies_expensive_password_checks(named_accounts, monkeypatch):
    authenticated = []

    def deny_password(_store, username, password):
        authenticated.append(username)
        return None

    monkeypatch.setattr(AccountStore, "authenticate", deny_password)
    with TestClient(create_app()) as client:
        payload = {"username": "not.registered", "password": "bad-credential"}
        for _ in range(LOGIN_ATTEMPTS_PER_MINUTE):
            assert client.post("/api/auth/login", json=payload).status_code == 401

        blocked = client.post("/api/auth/login", json=payload)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "60"
        assert len(authenticated) == LOGIN_ATTEMPTS_PER_MINUTE
        assert "password" not in blocked.text.lower()

        with named_accounts.connect() as conn:
            conn.execute(
                "UPDATE account_login_throttle "
                "SET window_started = now() - interval '61 seconds'"
            )
        assert client.post("/api/auth/login", json=payload).status_code == 401
        assert len(authenticated) == LOGIN_ATTEMPTS_PER_MINUTE + 1


def test_project_grants_rechecked_on_each_request(named_accounts):
    with TestClient(create_app()) as client:
        _login(client, "operator")
        assert client.get("/api/auth/me").json()["role"] == "operator"
        assert client.get("/api/operations").status_code == 200
        assert named_accounts.grant_local_role("sample.operator", "viewer")
        assert client.get("/api/auth/me").json()["role"] == "viewer"
        assert client.get("/api/oracle/proposals").status_code == 200
        assert client.get("/api/operations").status_code == 403
        assert client.post("/api/oracle/proposals", json={}).status_code == 403

        assert named_accounts.revoke_local_role("sample.operator")
        assert client.get("/api/auth/me").status_code == 403
        assert client.get("/api/runs").status_code == 403
        assert client.get("/api/oracle/proposals").status_code == 403
        assert client.post("/api/auth/logout").status_code == 200
        denied = client.post(
            "/api/auth/login",
            json={
                "username": "sample.operator",
                "password": "sample-long-local-password-555",
            },
        )
        assert denied.status_code == 403
        assert not client.cookies.get("clashbolt_identity")
        # CLI-only regrant restores the scoped permission, not any old session.
        assert named_accounts.grant_local_role("sample.operator", "operator")
        assert client.get("/api/runs").status_code == 401
        _login(client, "operator")
        assert client.get("/api/operations").status_code == 200


def test_membership_in_another_project_never_grants_local_resource_access(named_accounts):
    with named_accounts.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id) VALUES ('other') ON CONFLICT DO NOTHING"
        )
        account = conn.execute(
            "SELECT id FROM local_account WHERE username = 'sample.viewer'"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO project_membership(project_id, account_id, role)
            VALUES ('other', %s, 'viewer')
            """,
            (account["id"],),
        )
    assert named_accounts.revoke_local_role("sample.viewer")
    with TestClient(create_app()) as client:
        denied = client.post(
            "/api/auth/login",
            json={
                "username": "sample.viewer",
                "password": "sample-long-local-password-555",
            },
        )
        assert denied.status_code == 403
        assert client.get("/api/runs").status_code == 401
    # Merely existing as a member of another project never opens the local
    # project's API. The other project's resources are not exposed yet.


def test_readiness_fails_without_any_effective_local_grant(named_accounts):
    with TestClient(create_app()) as client:
        assert client.get("/api/ready").status_code == 200
        for username in ("sample.viewer", "sample.operator", "sample.reviewer"):
            assert named_accounts.revoke_local_role(username)
        readiness = client.get("/api/ready")
        assert readiness.status_code == 503
        assert "grant" in readiness.json()["detail"]
        assert named_accounts.grant_local_role("sample.viewer", "viewer")
        assert client.get("/api/ready").status_code == 200
