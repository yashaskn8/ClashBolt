"""Operational diagnostics are deterministic, scoped, and non-remediating."""

import os

import pytest
from clashbolt.api import create_app
from clashbolt.ops_alerts import diagnose_operations
from clashbolt.policy import Policy
from fastapi.testclient import TestClient


def _snapshot(*, pending=0, pending_age=0, workers=0, verifications=0):
    return {
        "runs": {"PENDING": pending},
        "verifications": {"PENDING": verifications},
        "responsive_workers": workers,
        "oldest_pending_seconds": pending_age,
    }


def test_no_false_incident_for_idle_local_workerless_lab():
    outcome = diagnose_operations(_snapshot())
    assert outcome["status"] == "NORMAL"
    assert outcome["alerts"] == []
    assert outcome["automatic_remediation"] is False
    assert outcome["notifications_sent"] is False


def test_pending_and_verification_backlogs_are_visible_without_a_worker():
    outcome = diagnose_operations(
        _snapshot(pending=3, pending_age=180, workers=0, verifications=2)
    )
    assert outcome["status"] == "ATTENTION"
    assert [item["code"] for item in outcome["alerts"]] == [
        "NO_RESPONSIVE_WORKER",
        "QUEUE_WAIT_OVER_TWO_MINUTES",
        "VERIFICATION_NEEDS_WORKER",
    ]
    outcome = diagnose_operations(_snapshot(pending=1, pending_age=120, workers=1))
    assert [item["code"] for item in outcome["alerts"]] == [
        "QUEUE_WAIT_OVER_TWO_MINUTES"
    ]


def test_operator_can_view_diagnostics_but_unauthenticated_cannot(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database required")
    store = Policy(dsn)
    store.migrate()
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "operations-ci-secret")
    with TestClient(create_app()) as client:
        assert client.get("/api/operations").status_code == 403
        response = client.get(
            "/api/operations", headers={"X-Operator-Token": "operations-ci-secret"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["diagnostics"]["status"] in ("NORMAL", "ATTENTION")
        assert payload["diagnostics"]["notifications_sent"] is False
        assert isinstance(payload["diagnostics"]["alerts"], list)


def test_quarantined_plan_alert_is_visible_to_authorized_operator(monkeypatch):
    from uuid import UUID

    from clashbolt.experiment import ORACLE_HASH
    from clashbolt.planner import choose_plan

    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL integration cluster required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, "
            "oracle_proposal CASCADE"
        )
    proposal = store.propose("quarantine-operator-alert")
    pid = UUID(proposal["id"])
    store.approve(pid, ORACLE_HASH, "reviewer")
    plan = store.record_plan(
        pid, choose_plan(strategy="semantic", variant="racy")
    )
    plan_id = UUID(plan["id"])
    with store.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET plan = jsonb_set(plan, '{schedule}', "
            "'\"serial\"'::jsonb) WHERE id = %s",
            (plan_id,),
        )
    assert store.reconcile_corrupt_plans() == 1
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "ci-local-incident-secret")
    with TestClient(create_app()) as client:
        assert client.get("/api/operations").status_code == 403
        response = client.get(
            "/api/operations",
            headers={"X-Operator-Token": "ci-local-incident-secret"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["quarantined_agent_plans"] >= 1
        assert "QUARANTINED_AGENT_CONTEXT" in {
            alert["code"] for alert in snapshot["diagnostics"]["alerts"]
        }
        metrics = client.get(
            "/api/metrics",
            headers={"X-Operator-Token": "ci-local-incident-secret"},
        )
        assert metrics.status_code == 200
        assert "clashbolt_quarantined_agent_plans 1" in metrics.text
    assert store.get_plan(plan_id)["state"] == "UNKNOWN"
