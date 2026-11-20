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
