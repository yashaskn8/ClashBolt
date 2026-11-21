"""P3 independent named review binds exact synthetic worker replay evidence."""

import asyncio
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.accounts import AccountStore
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.worker import process_one, process_verification_one
from fastapi.testclient import TestClient


@pytest.fixture
def reviewer_lab(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    accounts = AccountStore(dsn)
    accounts.migrate()
    with accounts.connect() as conn:
        conn.execute(
            "TRUNCATE account_login_throttle, local_account, "
            "oracle_proposal, experiment_run CASCADE"
        )
    for name, role in (
        ("review.operator", "operator"),
        ("review.reviewer", "reviewer"),
        ("review.viewer", "viewer"),
        ("review.other", "reviewer"),
    ):
        accounts.create_account(name, role, "review-password-long-enough")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv(
        "CLASHBOLT_SESSION_SECRET", "ci-reviewer-session-secret-32-character-key"
    )
    return accounts


def _signin(client, name):
    response = client.post(
        "/api/auth/login",
        json={"username": name, "password": "review-password-long-enough"},
    )
    assert response.status_code == 200, response.text


def _prepare_failed_run(client, accounts):
    _signin(client, "review.operator")
    proposal = client.post("/api/oracle/proposals", json={})
    assert proposal.status_code == 201, proposal.text
    oracle_id = proposal.json()["id"]
    _signin(client, "review.reviewer")
    approval = client.post(
        f"/api/oracle/proposals/{oracle_id}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "ignored"},
    )
    assert approval.status_code == 200, approval.text
    _signin(client, "review.operator")
    admitted = client.post(
        "/api/runs",
        json={
            "variant": "racy", "schedule": "overlap",
            "client_key": str(uuid4()), "approved_oracle_id": oracle_id,
        },
    )
    assert admitted.status_code == 201, admitted.text
    run_id = admitted.json()["run_id"]
    assert asyncio.run(process_one(Policy(accounts.dsn), "review-run-worker"))
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "COMPLETE"
    assert run["verdict"] == "FAIL"
    return oracle_id, run


def _review_body(run, decision="SUPPORTED", reason="Replay matches the synthetic witness"):
    return {
        "expected_run_version": run["version"],
        "expected_evidence_sha256": run["witness"]["evidence_sha256"],
        "decision": decision,
        "reason": reason,
    }


def test_review_requires_current_replay_and_is_immutable(reviewer_lab):
    accounts = reviewer_lab
    with TestClient(create_app()) as client:
        oracle_id, run = _prepare_failed_run(client, accounts)
        url = f"/api/runs/{run['run_id']}/reviews"
        assert client.post(url, json=_review_body(run)).status_code == 403
        _signin(client, "review.reviewer")
        assert client.post(url, json=_review_body(run)).status_code == 409
        before = client.get(url).json()
        assert before["replay_verified"] is False
        assert before["independent_external_reproduction"] is False

        _signin(client, "review.operator")
        job = client.post(
            f"/api/runs/{run['run_id']}/verification-jobs",
            json={"kind": "REPLAY"},
        )
        assert job.status_code == 202, job.text
        assert asyncio.run(
            process_verification_one(Policy(accounts.dsn), "review-verify-worker")
        )
        verified = client.get(
            f"/api/verification-jobs/{job.json()['job_id']}"
        ).json()
        assert verified["status"] == "COMPLETE"
        assert verified["result"]["status"] == "REPRODUCED"

        _signin(client, "review.reviewer")
        state = client.get(url).json()
        assert state["replay_verified"] is True
        assert state["replay_job_id"] == job.json()["job_id"]
        assert state["scope"] == "SYNTHETIC_ONLY"
        body = _review_body(run)
        first = client.post(url, json=body)
        assert first.status_code == 201, first.text
        assert client.post(url, json=body).json() == first.json()
        other = client.post(
            url, json=_review_body(run, "WITHHELD", "Different assessment of replay")
        )
        assert other.status_code == 409
        reviews = client.get(url).json()["reviews"]
        assert len(reviews) == 1
        assert reviews[0]["current"] is True
        assert reviews[0]["reviewer_account_id"] == (
            accounts.authenticate("review.reviewer", "review-password-long-enough")["id"]
        )

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with accounts.connect() as conn:
                conn.execute(
                    "UPDATE witness_review SET reason = 'modified record' "
                    "WHERE id = %s", (UUID(reviews[0]["review_id"]),),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with accounts.connect() as conn:
                conn.execute(
                    "DELETE FROM witness_review WHERE id = %s",
                    (UUID(reviews[0]["review_id"]),),
                )

        assert client.post(
            f"/api/oracle/proposals/{oracle_id}/revoke"
        ).status_code == 200
        withdrawn = client.get(url).json()["reviews"]
        assert len(withdrawn) == 1
        assert withdrawn[0]["current"] is False
        assert client.post(url, json=body).status_code == 409


def test_independent_role_and_cross_project_reviews_fail_closed(reviewer_lab):
    accounts = reviewer_lab
    project_id = "review-" + uuid4().hex[:14]
    accounts.create_project(project_id)
    accounts.grant_project_role("review.reviewer", "reviewer", project_id)
    accounts.grant_project_role("review.operator", "operator", project_id)
    with TestClient(create_app()) as client:
        _, run = _prepare_failed_run(client, accounts)
        url = f"/api/runs/{run['run_id']}/reviews"
        _signin(client, "review.viewer")
        assert client.get(url).status_code == 200
        assert client.post(
            url, json=_review_body(run, "WITHHELD", "Not independently replayed")
        ).status_code == 403

        _signin(client, "review.reviewer")
        cross = client.post(
            "/api/auth/select-project", json={"project_id": project_id}
        )
        assert cross.status_code == 200, cross.text
        assert client.get(url).status_code == 404
        assert client.post(
            url, json=_review_body(run, "WITHHELD", "No tenant-bound evidence")
        ).status_code == 409
        assert client.post(
            "/api/auth/select-project", json={"project_id": "local"}
        ).status_code == 200

        bad = client.post(
            url, json={
                **_review_body(run),
                "expected_evidence_sha256": "a" * 64,
                "decision": "WITHHELD",
            },
        )
        assert bad.status_code == 409
        supported_without_replay = client.post(url, json=_review_body(run))
        assert supported_without_replay.status_code == 409
        withheld = client.post(
            url, json=_review_body(run, "WITHHELD", "Evidence cannot be independently replayed")
        )
        assert withheld.status_code == 201, withheld.text
        assert client.get(url).json()["reviews"][0]["decision"] == "WITHHELD"

        _signin(client, "review.other")
        assert client.post(url, json=_review_body(run)).status_code == 409
        assert len(client.get(url).json()["reviews"]) == 1
