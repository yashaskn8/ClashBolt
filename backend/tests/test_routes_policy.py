import os
from uuid import UUID

import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from fastapi.testclient import TestClient


@pytest.fixture
def policy_client(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database not configured")
    policy = Policy(dsn)
    policy.migrate()
    with policy.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "ci-local-only-secret")
    with TestClient(create_app()) as client:
        yield client


def test_approval_and_tool_authority(policy_client):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "planner"}
    ).json()
    pid = proposal["id"]
    approval = {"expected_hash": ORACLE_HASH, "reviewer": "reviewer"}
    url = f"/api/oracle/proposals/{pid}/approve"
    assert policy_client.post(url, json=approval).status_code == 403
    header = {"X-Operator-Token": "ci-local-only-secret"}
    assert policy_client.post(url, json={**approval, "expected_hash": "0" * 64},
                              headers=header).status_code == 409
    assert policy_client.post(url, json=approval, headers=header).status_code == 200
    planned = policy_client.post(
        "/api/agent/plans", json={
            "proposal_id": pid, "strategy": "semantic", "variant": "racy", "seed": 0
        },
    ).json()
    plan_id = planned["id"]
    capability = policy_client.post(
        "/api/tool-capabilities", json={
            "proposal_id": pid, "variant": "racy", "schedule": "overlap",
            "max_calls": 1, "plan_id": plan_id,
        }, headers=header,
    ).json()
    token = capability["token"]
    tool = "/api/agent/tools/run"
    assert policy_client.post(
        tool, json={"variant": "racy", "schedule": "serial", "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 403
    response = policy_client.post(
        tool, json={"variant": "racy", "schedule": "overlap", "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "FAIL"
    saved = policy_client.get(f"/api/agent/plans/{plan_id}").json()
    assert saved["state"] == "COMPLETE"
    assert saved["result"]["verdict"] == "FAIL"
    assert policy_client.post(
        tool, json={"variant": "racy", "schedule": "overlap", "plan_id": plan_id},
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 403
    assert policy_client.get("/api/tool-audit", headers=header).json()["events"]


def test_rule_revocation_denies_existing_capability(policy_client):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "planner"}
    ).json()
    pid = proposal["id"]
    header = {"X-Operator-Token": "ci-local-only-secret"}
    approved = policy_client.post(
        f"/api/oracle/proposals/{pid}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "reviewer"},
        headers=header,
    )
    assert approved.status_code == 200
    issue = policy_client.post(
        "/api/tool-capabilities",
        json={"proposal_id": pid, "variant": "locked", "schedule": "serial"},
        headers=header,
    )
    assert issue.status_code == 201
    secret = issue.json()["token"]
    assert policy_client.post(
        f"/api/oracle/proposals/{pid}/revoke", headers=header
    ).status_code == 200
    assert policy_client.post(
        "/api/agent/tools/run",
        json={"variant": "locked", "schedule": "serial", "plan_id": str(UUID(int=0))},
        headers={"Authorization": f"Bearer {secret}"},
    ).status_code == 403


def test_deterministic_plan_and_local_evaluation(policy_client):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "planner"}
    ).json()
    plan_body = {
        "proposal_id": proposal["id"], "strategy": "semantic",
        "variant": "racy", "seed": 0
    }
    assert policy_client.post("/api/agent/plans", json=plan_body).status_code == 409
    header = {"X-Operator-Token": "ci-local-only-secret"}
    policy_client.post(
        f"/api/oracle/proposals/{proposal['id']}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "reviewer"},
        headers=header,
    )
    result = policy_client.post("/api/agent/plans", json=plan_body)
    assert result.status_code == 201
    assert result.json()["plan"]["planner_kind"] == "DETERMINISTIC_BASELINE"
    comparison = policy_client.post(
        "/api/agent/evaluate", json={"trials": 3}, headers=header,
    )
    assert comparison.status_code == 200
    assert comparison.json()["strategies"]["semantic"]["controlled_failures"] == 3
