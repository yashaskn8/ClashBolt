"""Project-scoped run previews are advisory, non-mutating, and fail closed."""

import os
from uuid import UUID, uuid4

import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from fastapi.testclient import TestClient


@pytest.fixture
def preview_lab(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute("TRUNCATE oracle_proposal, experiment_run CASCADE")
        conn.execute(
            """
            INSERT INTO project_quota(project_id, max_open)
            VALUES ('local', 2)
            ON CONFLICT (project_id) DO UPDATE SET max_open = 2
            """
        )
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "preview-local-token")
    monkeypatch.delenv("CLASHBOLT_TARGET_CLUSTER_URL", raising=False)
    monkeypatch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)
    return store


def _preview(client, approved_id=None, execution_backend="memory"):
    return client.post(
        "/api/runs/preview",
        json={
            "variant": "racy",
            "schedule": "overlap",
            "step_budget": 4,
            "approved_oracle_id": approved_id,
            "execution_backend": execution_backend,
        },
        headers={"X-Operator-Token": "preview-local-token"},
    )


def test_advisory_preview_cannot_grant_approval_or_reserve_quota(preview_lab):
    with TestClient(create_app()) as client:
        assert client.post("/api/runs/preview", json={
            "variant": "racy", "schedule": "overlap",
        }).status_code == 403
        response = _preview(client)
        assert response.status_code == 200, response.text
        unapproved = response.json()
        assert unapproved["preflight_ok"] is False
        assert unapproved["reservation_made"] is False
        assert unapproved["blockers"] == [
            "No current approved oracle for this project"
        ]
        assert unapproved["max_open"] == 2
        assert unapproved["open_runs"] == 0
        assert unapproved["resource_bound"] == {
            "checkpoint_limit": 4,
            "disposable_databases": 0,
            "cpu_seconds_estimate": None,
        }

        proposal = preview_lab.propose("preview-author")
        pid = UUID(proposal["id"])
        preview_lab.approve(pid, ORACLE_HASH, "preview-reviewer")
        for _ in range(2):
            current = _preview(client, proposal["id"])
            assert current.status_code == 200, current.text
            assert current.json()["preflight_ok"] is True
            result = client.post(
                "/api/runs",
                json={
                    "client_key": str(uuid4()), "variant": "racy",
                    "schedule": "overlap", "approved_oracle_id": proposal["id"],
                },
                headers={"X-Operator-Token": "preview-local-token"},
            )
            assert result.status_code == 201, result.text

        exhausted = _preview(client, proposal["id"]).json()
        assert exhausted["preflight_ok"] is False
        assert exhausted["open_runs"] == exhausted["max_open"] == 2
        assert "Active experiment quota is full" in exhausted["blockers"]
        with preview_lab.connect() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM experiment_run"
            ).fetchone()["n"]
        assert count == 2
        assert _preview(client, str(uuid4())).json()["oracle_approved"] is False


def test_native_preview_reports_configuration_not_security_certification(preview_lab):
    with TestClient(create_app()) as client:
        preview = _preview(client, execution_backend="native-postgresql")
        assert preview.status_code == 200, preview.text
        result = preview.json()
        assert result["backend_configured"] is False
        assert result["resource_bound"]["disposable_databases"] == 1
        assert result["resource_bound"]["cpu_seconds_estimate"] is None
        assert "Separate native target cluster is not configured" in result["blockers"]
        # A configured URL that aliases the control cluster is also refused.
        os.environ["CLASHBOLT_TARGET_CLUSTER_URL"] = preview_lab.dsn
        try:
            invalid_target = _preview(
                client, execution_backend="native-postgresql"
            ).json()
            assert invalid_target["backend_configured"] is False
            assert invalid_target["preflight_ok"] is False
        finally:
            del os.environ["CLASHBOLT_TARGET_CLUSTER_URL"]
        malformed = client.post(
            "/api/runs/preview",
            json={"variant": "racy", "schedule": "invalid"},
            headers={"X-Operator-Token": "preview-local-token"},
        )
        assert malformed.status_code == 422
        assert _preview(client, str(uuid4())).status_code == 200
