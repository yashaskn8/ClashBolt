"""Durable timeline cursors and streaming permissions on native PostgreSQL."""

import os
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.store import MAX_EVENT_VERSION, Ledger
from fastapi.testclient import TestClient


@pytest.fixture
def timeline(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL required")
    accounts = AccountStore(dsn)
    accounts.migrate()
    with accounts.connect() as conn:
        conn.execute(
            "TRUNCATE account_login_throttle, local_account, "
            "oracle_proposal, experiment_run CASCADE"
        )
    accounts.create_account("timeline.viewer", "viewer", "local-timeline-viewer-password")
    proposal = accounts.propose("timeline-operator")
    rule = UUID(proposal["id"])
    accounts.approve(rule, ORACLE_HASH, "timeline-reviewer")
    run_id = UUID(accounts.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial", approved_oracle_id=rule,
    )["run_id"])
    assert accounts.cancel(run_id, accounts.get(run_id)['version'])["status"] == "CANCELLED"
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "timeline-local-session-key-for-tests")
    with TestClient(create_app()) as client:
        authenticated = client.post("/api/auth/login", json={
            "username": "timeline.viewer", "password": "local-timeline-viewer-password",
        })
        assert authenticated.status_code == 200
        yield accounts, client, f"/api/runs/{run_id}", run_id


def test_paginated_events_keep_stable_cursors_and_bound_integer_input(timeline):
    accounts, client, url, run_id = timeline
    first = client.get(f"{url}/events", params={"limit": 1}).json()
    assert first["run_id"] == str(run_id)
    assert first["events"][0]["status"] == "PENDING"
    assert first["next_after"] == 1
    assert first["has_more"] is True
    second = client.get(f"{url}/events", params={"after": 1, "limit": 1}).json()
    assert second["events"][0]["status"] == "CANCELLED"
    assert second["next_after"] == 2
    assert second["has_more"] is False
    at_limit = client.get(f"{url}/events", params={"after": MAX_EVENT_VERSION}).json()
    assert at_limit["events"] == []
    assert at_limit["next_after"] == MAX_EVENT_VERSION
    for after, limit in [(-1, 1), (MAX_EVENT_VERSION + 1, 1), (0, 101)]:
        response = client.get(f"{url}/events", params={"after": after, "limit": limit})
        assert response.status_code == 422
        with pytest.raises(ValueError, match="cursor or page limit"):
            accounts.transition_page(run_id, after, limit)
    assert client.get(f"/api/runs/{uuid4()}/events").status_code == 404


@pytest.mark.parametrize("last_id", ["", "-1", "invalid", str(MAX_EVENT_VERSION + 1)])
def test_stream_rejects_invalid_resume_header(timeline, last_id):
    _, client, url, _ = timeline
    assert client.get(f"{url}/stream", headers={"Last-Event-ID": last_id}).status_code == 422


@pytest.mark.parametrize("revocation,status", [("grant", 403), ("session", 401)])
def test_stream_resumes_then_closes_when_access_is_revoked(
    timeline, monkeypatch, revocation, status,
):
    accounts, client, url, _ = timeline
    original = Ledger.transition_page

    def revoke_after_read(ledger, run_id, after=0, limit=100):
        page = original(ledger, run_id, after, limit)
        if revocation == "grant":
            assert accounts.revoke_local_role("timeline.viewer")
        else:
            assert accounts.revoke_sessions(ledger.actor_account_id)
        return page

    monkeypatch.setattr(Ledger, "transition_page", revoke_after_read)
    event = client.get(f"{url}/stream", headers={"Last-Event-ID": "1"})
    assert event.status_code == 200
    assert event.headers["content-type"].startswith("text/event-stream")
    assert "id: 2\nevent: transition" in event.text
    assert event.text.count("event: transition") == 1
    assert "CANCELLED" in event.text
    assert client.get(f"{url}/events").status_code == status


def test_account_mode_disables_process_global_scratch_and_legacy_unlock(timeline):
    _, client, _, _ = timeline
    assert client.post("/api/session").status_code == 404
    assert client.post("/api/experiments", json={
        "variant": "locked", "schedule": "serial",
    }).status_code == 403
    assert client.get("/api/experiments/missing").status_code == 404
    denied = client.post("/api/auth/select-project", json={"project_id": "missing"})
    assert denied.status_code == 403


def test_timeline_reports_missing_runs_and_invalid_stream_cursors(timeline):
    _, client, url, _ = timeline
    assert client.get(f"/api/runs/{uuid4()}/stream").status_code == 404
    for after in (-1, MAX_EVENT_VERSION + 1):
        assert client.get(f"{url}/stream", params={"after": after}).status_code == 422
