"""Real account/worker/API checks for isolated PostgreSQL project workspaces."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.worker import process_one
from fastapi.testclient import TestClient


@pytest.fixture
def workspace(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL test database required")
    store = AccountStore(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE account_login_throttle, local_account, "
            "oracle_proposal, experiment_run CASCADE"
        )
    store.create_account(
        "multi.operator", "operator", "workspace-login-password-1234"
    )
    store.create_account(
        "multi.reviewer", "reviewer", "workspace-login-password-1234"
    )
    project_id = "tenant-" + uuid4().hex[:12]
    schema = store.create_project(project_id)
    assert schema.startswith("cbp_")
    assert store.project_schema(project_id) == schema
    assert store.grant_project_role("multi.operator", "operator", project_id)
    assert store.grant_project_role("multi.reviewer", "reviewer", project_id)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv(
        "CLASHBOLT_SESSION_SECRET", "ci-multiple-projects-32-character-secret"
    )
    return store, project_id, schema


def _sign_in(client, role):
    response = client.post(
        "/api/auth/login",
        json={
            "username": f"multi.{role}",
            "password": "workspace-login-password-1234",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == "local"


def _select(client, project_id, role):
    response = client.post(
        "/api/auth/select-project", json={"project_id": project_id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == project_id
    assert response.json()["role"] == role


def _approve(client, proposal_id, project_id):
    _sign_in(client, "reviewer")
    _select(client, project_id, "reviewer")
    response = client.post(
        f"/api/oracle/proposals/{proposal_id}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "ignored-body"},
    )
    assert response.status_code == 200, response.text


def test_independent_project_oracles_runs_caps_and_worker(workspace):
    store, project_id, schema = workspace
    with TestClient(create_app()) as client:
        _sign_in(client, "operator")
        projects = client.get("/api/auth/projects").json()["projects"]
        assert {item["id"] for item in projects} == {"local", project_id}
        local_rule = client.post("/api/oracle/proposals", json={})
        assert local_rule.status_code == 201, local_rule.text
        local_id = local_rule.json()["id"]
        _approve(client, local_id, "local")

        _sign_in(client, "operator")
        local_run = client.post(
            "/api/runs",
            json={
                "client_key": str(uuid4()), "variant": "locked",
                "schedule": "serial", "approved_oracle_id": local_id,
            },
        )
        assert local_run.status_code == 201, local_run.text
        local_run_id = local_run.json()["run_id"]

        _select(client, project_id, "operator")
        assert client.get("/api/oracle/proposals").json()["proposals"] == []
        assert client.get("/api/runs").json()["runs"] == []
        assert client.get(f"/api/runs/{local_run_id}").status_code == 404
        assert client.get(f"/api/runs/{local_run_id}/bundle").status_code == 404
        assert client.post(
            f"/api/runs/{local_run_id}/cancel", json={"expected_run_version": 1},
        ).status_code == 404

        tenant_rule = client.post("/api/oracle/proposals", json={})
        assert tenant_rule.status_code == 201, tenant_rule.text
        tenant_id = tenant_rule.json()["id"]
        _approve(client, tenant_id, project_id)

        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        tenant_run = client.post(
            "/api/runs",
            json={
                "client_key": str(uuid4()), "variant": "racy",
                "schedule": "overlap", "approved_oracle_id": tenant_id,
            },
        )
        assert tenant_run.status_code == 201, tenant_run.text
        tenant_run_id = tenant_run.json()["run_id"]
        assert tenant_run.json()["project_id"] == project_id

        planned = client.post(
            "/api/agent/plans", json={
                "proposal_id": tenant_id, "strategy": "semantic",
                "variant": "racy", "seed": 0,
            },
        )
        assert planned.status_code == 201, planned.text
        plan_id = planned.json()["id"]
        issued = client.post(
            "/api/tool-capabilities", json={
                "proposal_id": tenant_id, "variant": "racy",
                "schedule": "overlap", "plan_id": plan_id,
            },
        )
        assert issued.status_code == 201, issued.text
        token = issued.json()["token"]

        _select(client, "local", "operator")
        assert client.get(f"/api/runs/{tenant_run_id}").status_code == 404
        assert client.get(f"/api/agent/plans/{plan_id}").status_code == 404
        assert not any(
            rule["id"] == tenant_id
            for rule in client.get("/api/oracle/proposals").json()["proposals"]
        )
        tool_request = {
            "variant": "racy", "schedule": "overlap", "plan_id": plan_id,
        }
        assert client.post(
            "/api/agent/tools/run", json=tool_request,
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 403

        _select(client, project_id, "operator")
        completed = client.post(
            "/api/agent/tools/run", json=tool_request,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["verdict"] == "FAIL"

        assert asyncio.run(process_one(
            Policy(store.dsn, schema=schema, project_id=project_id),
            "ci-scoped-tenant-worker",
        ))
        run = client.get(f"/api/runs/{tenant_run_id}")
        assert run.status_code == 200
        assert run.json()["status"] == "COMPLETE"
        assert run.json()["verdict"] == "FAIL"

        # Both projects retain their own complete history and no extra rows.
        _select(client, "local", "operator")
        assert client.get(f"/api/runs/{local_run_id}").status_code == 200
        assert client.get(f"/api/runs/{tenant_run_id}").status_code == 404
        with store.connect() as conn:
            public_count = conn.execute(
                "SELECT count(*) AS n FROM oracle_proposal WHERE id = %s",
                (UUID(tenant_id),),
            ).fetchone()["n"]
        assert public_count == 0
        with Policy(store.dsn, schema=schema, project_id=project_id).connect() as conn:
            tenant_count = conn.execute(
                "SELECT count(*) AS n FROM oracle_proposal WHERE id = %s",
                (UUID(local_id),),
            ).fetchone()["n"]
        assert tenant_count == 0


def test_project_role_revocation_rejects_old_tool_and_project_session(workspace):
    store, project_id, _ = workspace
    with TestClient(create_app()) as client:
        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        proposal = client.post("/api/oracle/proposals", json={})
        assert proposal.status_code == 201
        _approve(client, proposal.json()["id"], project_id)

        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        planned = client.post(
            "/api/agent/plans", json={
                "proposal_id": proposal.json()["id"], "strategy": "semantic",
                "variant": "racy", "seed": 0,
            },
        )
        assert planned.status_code == 201
        issued = client.post(
            "/api/tool-capabilities", json={
                "proposal_id": proposal.json()["id"], "variant": "racy",
                "schedule": "overlap", "plan_id": planned.json()["id"],
            },
        )
        assert issued.status_code == 201, issued.text
        token = issued.json()["token"]

        assert store.revoke_project_role("multi.operator", project_id)
        assert client.get("/api/auth/me").status_code == 403
        assert client.get("/api/runs").status_code == 403
        assert client.get("/api/auth/projects").status_code == 200
        _select(client, "local", "operator")
        assert client.get("/api/auth/me").status_code == 200
        assert store.grant_project_role("multi.operator", "operator", project_id)
        _select(client, project_id, "operator")
        denied = client.post(
            "/api/agent/tools/run",
            json={
                "variant": "racy", "schedule": "overlap",
                "plan_id": planned.json()["id"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 403
        with Policy(
            store.dsn, schema=store.project_schema(project_id), project_id=project_id
        ).connect() as conn:
            row = conn.execute(
                "SELECT revoked, used_calls FROM tool_capability WHERE id = %s",
                (UUID(issued.json()["capability_id"]),),
            ).fetchone()
        assert row == {"revoked": True, "used_calls": 0}


def test_transaction_scope_fences_revocation_after_middleware_check(workspace):
    store, project_id, schema = workspace
    with store.connect() as conn:
        account_id = conn.execute(
            "SELECT id FROM local_account WHERE username = 'multi.operator'"
        ).fetchone()["id"]
    scoped = Policy(
        store.dsn, schema=schema, project_id=project_id,
        actor_account_id=account_id, required_role="operator",
    )
    assert scoped.list_proposals() == []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with scoped.connect() as conn:
            # This is an already-authorized request that has entered its
            # transaction. A competing grant revocation must wait for it.
            assert conn.execute(
                "SELECT count(*) AS count FROM oracle_proposal"
            ).fetchone()["count"] == 0
            revoked = pool.submit(
                store.revoke_project_role, "multi.operator", project_id,
            )
            with pytest.raises(FutureTimeout):
                revoked.result(timeout=0.2)
        assert revoked.result(timeout=5) is True

    # The previously constructed scoped store cannot reuse a stale
    # middleware decision after revocation has committed.
    with pytest.raises(PermissionError, match="access changed"):
        scoped.propose("operator-with-old-session")
    with pytest.raises(PermissionError, match="access changed"):
        scoped.list_proposals()

    assert store.grant_project_role("multi.operator", "viewer", project_id)
    with pytest.raises(PermissionError, match="access changed"):
        scoped.propose("downgraded-operator")
    viewer = Policy(
        store.dsn, schema=schema, project_id=project_id,
        actor_account_id=account_id, required_role="viewer",
    )
    assert viewer.list_proposals() == []


def test_per_account_drafts_are_locked_against_grant_changes(workspace):
    store, project_id, _ = workspace
    with store.connect() as conn:
        account_id = conn.execute(
            "SELECT id FROM local_account WHERE username = 'multi.operator'"
        ).fetchone()["id"]
    draft = {
        "variant": "racy", "schedule": "serial",
        "execution_backend": "memory",
    }
    saved = store.save_account_draft(account_id, draft, 0, project_id)
    assert saved["version"] == 1
    assert store.get_account_draft(account_id, project_id)["draft"] == draft
    assert store.grant_project_role("multi.operator", "viewer", project_id)
    assert store.get_account_draft(account_id, project_id)["version"] == 1
    with pytest.raises(PermissionError, match="draft transaction"):
        store.save_account_draft(account_id, dict(draft, variant="locked"), 1, project_id)

    assert store.revoke_project_role("multi.operator", project_id)
    with pytest.raises(PermissionError, match="draft transaction"):
        store.get_account_draft(account_id, project_id)
    assert store.grant_project_role("multi.operator", "operator", project_id)
    assert store.get_account_draft(account_id, project_id)["draft"] == draft


def test_admission_preview_remains_in_selected_project(workspace):
    store, project_id, schema = workspace
    tenant = Policy(store.dsn, schema=schema, project_id=project_id)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO project_quota(project_id, max_open)
            VALUES ('local', 7)
            ON CONFLICT (project_id) DO UPDATE SET max_open = 7
            """
        )
    with tenant.connect() as conn:
        conn.execute(
            """
            INSERT INTO project_quota(project_id, max_open)
            VALUES ('local', 3)
            ON CONFLICT (project_id) DO UPDATE SET max_open = 3
            """
        )
    payload = {"variant": "locked", "schedule": "serial"}
    with TestClient(create_app()) as client:
        _sign_in(client, "operator")
        local = client.post("/api/runs/preview", json=payload)
        assert local.status_code == 200, local.text
        assert local.json()["project_id"] == "local"
        assert local.json()["max_open"] == 7

        _select(client, project_id, "operator")
        scoped = client.post("/api/runs/preview", json=payload)
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["project_id"] == project_id
        assert scoped.json()["max_open"] == 3
        assert scoped.json()["reservation_made"] is False

        _sign_in(client, "reviewer")
        _select(client, project_id, "reviewer")
        assert client.post("/api/runs/preview", json=payload).status_code == 403


@pytest.mark.parametrize("revocation", ["grant", "session"])
def test_verification_admission_rechecks_project_and_session_at_write(
    workspace, monkeypatch, revocation,
):
    from clashbolt.verification_jobs import VerificationJobs

    store, project_id, schema = workspace
    project = Policy(store.dsn, project_id=project_id, schema=schema)
    oracle_id = UUID(project.propose("fixture-author")["id"])
    project.approve(oracle_id, ORACLE_HASH, "reviewer")
    run_id = project.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=oracle_id,
    )["run_id"]
    assert asyncio.run(process_one(project, "verification-permissions-worker"))
    with store.connect() as conn:
        account_id = conn.execute(
            "SELECT id FROM local_account WHERE username = 'multi.operator'"
        ).fetchone()["id"]
    original = VerificationJobs.submit

    def submit_after_revocation(queue, run, kind):
        if revocation == "grant":
            assert store.revoke_project_role("multi.operator", project_id)
        else:
            assert store.revoke_sessions(account_id)
        return original(queue, run, kind)

    monkeypatch.setattr(VerificationJobs, "submit", submit_after_revocation)
    with TestClient(create_app()) as client:
        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        response = client.post(f"/api/runs/{run_id}/verification-jobs", json={"kind": "REPLAY"})
        assert response.status_code == 403, response.text
    with project.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM verification_job").fetchone()["n"] == 0


def test_draft_update_is_rejected_if_session_changes_before_write(workspace, monkeypatch):
    store, project_id, _ = workspace
    original = AccountStore.save_account_draft

    def save_after_logout(accounts, account_id, *args, **kwargs):
        assert store.revoke_sessions(account_id)
        return original(accounts, account_id, *args, **kwargs)

    monkeypatch.setattr(AccountStore, "save_account_draft", save_after_logout)
    with TestClient(create_app()) as client:
        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        update = client.post("/api/experiment-draft", json={
            "expected_version": 0, "variant": "locked", "schedule": "serial",
        })
        assert update.status_code == 403, update.text
        _sign_in(client, "operator")
        _select(client, project_id, "operator")
        assert client.get("/api/experiment-draft").json() == {"saved": None}
