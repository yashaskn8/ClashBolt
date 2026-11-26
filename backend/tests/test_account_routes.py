import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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


def test_foreign_project_run_references_are_not_visible_through_local_api(
    named_accounts,
):
    foreign_id = uuid4()
    with named_accounts.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id) VALUES ('foreign') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            """
            INSERT INTO experiment_run(
                id, project_id, client_key, request_hash, variant, schedule,
                oracle_hash, step_budget
            ) VALUES (%s, 'foreign', %s, %s, 'locked', 'serial', %s, 4)
            """,
            (foreign_id, str(uuid4()), str(uuid4()), ORACLE_HASH),
        )
    with TestClient(create_app()) as client:
        _login(client, "operator")
        assert all(
            row["run_id"] != str(foreign_id)
            for row in client.get("/api/runs").json()["runs"]
        )
        for path in (
            f"/api/runs/{foreign_id}",
            f"/api/runs/{foreign_id}/events",
            f"/api/runs/{foreign_id}/bundle",
            f"/api/runs/{foreign_id}/reduction",
        ):
            assert client.get(path).status_code == 404
        assert client.post(
        f"/api/runs/{foreign_id}/cancel", json={"expected_run_version": 1},
    ).status_code == 404
    with named_accounts.connect() as conn:
        status = conn.execute(
            "SELECT status FROM experiment_run WHERE id = %s", (foreign_id,),
        ).fetchone()["status"]
    assert status == "PENDING"


def test_downgraded_reviewer_cannot_approve_with_existing_signed_session(
    named_accounts,
):
    with TestClient(create_app()) as client:
        _login(client, "operator")
        proposal = client.post(
            "/api/oracle/proposals", json={"proposed_by": "operator"}
        ).json()
        approve_path = f"/api/oracle/proposals/{proposal['id']}/approve"
        payload = {"expected_hash": ORACLE_HASH, "reviewer": "reviewer"}
        _login(client, "reviewer")
        assert named_accounts.grant_local_role("sample.reviewer", "viewer")
        assert client.get("/api/auth/me").json()["role"] == "viewer"
        assert client.post(approve_path, json=payload).status_code == 403
        with named_accounts.connect() as conn:
            state = conn.execute(
                "SELECT state FROM oracle_proposal WHERE id = %s",
                (UUID(proposal["id"]),),
            ).fetchone()["state"]
        assert state == "PROPOSED"
        assert named_accounts.grant_local_role("sample.reviewer", "reviewer")
        approved = client.post(approve_path, json=payload)
        assert approved.status_code == 200, approved.text


def _issue_operator_capability(client: TestClient) -> tuple[str, dict]:
    """A reviewer must approve before an operator can issue a scoped tool."""
    _login(client, "operator")
    proposal = client.post("/api/oracle/proposals", json={})
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["id"]

    _login(client, "reviewer")
    approved = client.post(
        f"/api/oracle/proposals/{proposal_id}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "not-trusted-body"},
    )
    assert approved.status_code == 200, approved.text

    _login(client, "operator")
    plan = client.post("/api/agent/plans", json={
        "proposal_id": proposal_id,
        "strategy": "semantic", "variant": "racy", "seed": 0,
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    grant = client.post("/api/tool-capabilities", json={
        "proposal_id": proposal_id,
        "variant": "racy", "schedule": "overlap",
        "max_calls": 1, "plan_id": plan_id,
    })
    assert grant.status_code == 201, grant.text
    return plan_id, grant.json()


def test_account_bound_tool_capability_tracks_issuer_and_refuses_revocation(
    named_accounts,
):
    with TestClient(create_app()) as client:
        plan_id, cap = _issue_operator_capability(client)
        with named_accounts.connect() as conn:
            row = conn.execute(
                "SELECT issuer_account_id, issuer_project_id, revoked, used_calls "
                "FROM tool_capability WHERE id = %s",
                (UUID(cap["capability_id"]),),
            ).fetchone()
            account = conn.execute(
                "SELECT id FROM local_account WHERE username = 'sample.operator'"
            ).fetchone()
        assert row["issuer_account_id"] == account["id"]
        assert row["issuer_project_id"] == "local"
        assert row["revoked"] is False

        assert named_accounts.revoke_local_role("sample.operator")
        assert named_accounts.grant_local_role("sample.operator", "operator")
        request = {"variant": "racy", "schedule": "overlap", "plan_id": plan_id}
        # The capability-only route deliberately skips cookie authentication,
        # so *only* policy's live membership and persisted revocation protect it.
        denied = client.post(
            "/api/agent/tools/run", json=request,
            headers={"Authorization": f"Bearer {cap['token']}"},
        )
        assert denied.status_code == 403, denied.text
        with named_accounts.connect() as conn:
            updated = conn.execute(
                "SELECT revoked, used_calls FROM tool_capability WHERE id = %s",
                (UUID(cap["capability_id"]),),
            ).fetchone()
        assert updated == {"revoked": True, "used_calls": 0}
        assert named_accounts.get_plan(UUID(plan_id))["state"] == "PROPOSED"

        # The regrant may authorize a *new* capability, not resurrect the old.
        fresh = client.post("/api/tool-capabilities", json={
            "proposal_id": named_accounts.get_plan(UUID(plan_id))["proposal_id"],
            "variant": "racy", "schedule": "overlap",
            "max_calls": 1, "plan_id": plan_id,
        })
        assert fresh.status_code == 201, fresh.text
        completed = client.post(
            "/api/agent/tools/run", json=request,
            headers={"Authorization": f"Bearer {fresh.json()['token']}"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["verdict"] == "FAIL"


@pytest.mark.parametrize("change", ["downgrade", "disable", "reset", "logout"])
def test_issuer_role_and_credential_changes_revoke_live_bearers(
    named_accounts, change,
):
    with TestClient(create_app()) as client:
        plan_id, cap = _issue_operator_capability(client)
        if change == "downgrade":
            assert named_accounts.grant_local_role("sample.operator", "viewer")
        elif change == "disable":
            assert named_accounts.disable_account("sample.operator")
        elif change == "reset":
            assert named_accounts.reset_password(
                "sample.operator", "replacement-long-password-54321"
            )
        else:
            assert client.post("/api/auth/logout").status_code == 200
        denied = client.post(
            "/api/agent/tools/run",
            json={"variant": "racy", "schedule": "overlap", "plan_id": plan_id},
            headers={"Authorization": f"Bearer {cap['token']}"},
        )
        assert denied.status_code == 403
        with named_accounts.connect() as conn:
            row = conn.execute(
                "SELECT revoked, used_calls FROM tool_capability WHERE id = %s",
                (UUID(cap["capability_id"]),),
            ).fetchone()
        assert row == {"revoked": True, "used_calls": 0}


def test_named_capability_authorization_waits_for_revoking_account(
    named_accounts,
):
    """A locked account can revoke a cap without a cap/account deadlock."""
    with TestClient(create_app()) as client:
        plan_id, cap = _issue_operator_capability(client)
    with named_accounts.connect() as conn:
        issuer = conn.execute(
            "SELECT id FROM local_account WHERE username = 'sample.operator'"
        ).fetchone()["id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with named_accounts.connect() as conn:
            conn.execute(
                "SELECT id FROM local_account WHERE id = %s FOR UPDATE",
                (issuer,),
            )
            future = pool.submit(
                named_accounts.authorize,
                cap["token"], "racy", "overlap", UUID(plan_id),
                accounts_mode=True,
            )
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.2)
            conn.execute(
                "DELETE FROM project_membership "
                "WHERE account_id = %s AND project_id = 'local'",
                (issuer,),
            )
            conn.execute(
                "UPDATE tool_capability SET revoked = TRUE "
                "WHERE issuer_account_id = %s",
                (issuer,),
            )
        assert future.result(timeout=5) is False
    with named_accounts.connect() as conn:
        row = conn.execute(
            "SELECT used_calls FROM tool_capability WHERE id = %s",
            (UUID(cap["capability_id"]),),
        ).fetchone()
    assert row["used_calls"] == 0
    assert named_accounts.get_plan(UUID(plan_id))["state"] == "PROPOSED"


def test_legacy_tool_capability_is_not_accepted_in_named_account_mode(
    named_accounts,
):
    proposal = named_accounts.propose("legacy-capability-no-named-issuer")
    named_accounts.approve(UUID(proposal["id"]), ORACLE_HASH, "trusted-reviewer")
    from clashbolt.planner import choose_plan

    plan = named_accounts.record_plan(
        UUID(proposal["id"]), choose_plan(strategy="semantic", variant="racy")
    )
    plan_id = UUID(plan["id"])
    cap = named_accounts.issue(
        UUID(proposal["id"]), "racy", "overlap", plan_id=plan_id,
    )
    with TestClient(create_app()) as client:
        denied = client.post(
            "/api/agent/tools/run",
            json={
                "variant": "racy", "schedule": "overlap",
                "plan_id": str(plan_id),
            },
            headers={"Authorization": f"Bearer {cap['token']}"},
        )
        assert denied.status_code == 403
    with named_accounts.connect() as conn:
        used = conn.execute(
            "SELECT used_calls FROM tool_capability WHERE id = %s",
            (UUID(cap["capability_id"]),),
        ).fetchone()["used_calls"]
    assert used == 0
