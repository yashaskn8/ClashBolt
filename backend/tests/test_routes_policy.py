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


def test_separate_reviewer_credential_is_required(monkeypatch, policy_client):
    monkeypatch.setenv("CLASHBOLT_REVIEWER_TOKEN", "separate-reviewer-secret")
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "planner"}
    ).json()
    pid = proposal["id"]
    request = {"expected_hash": ORACLE_HASH, "reviewer": "separate-reviewer"}
    url = f"/api/oracle/proposals/{pid}/approve"
    assert policy_client.post(
        url, json=request, headers={"X-Operator-Token": "ci-local-only-secret"}
    ).status_code == 403
    assert policy_client.post(
        url, json=request, headers={"X-Reviewer-Token": "ci-local-only-secret"}
    ).status_code == 403
    assert policy_client.post(
        url, json=request, headers={"X-Reviewer-Token": "separate-reviewer-secret"}
    ).status_code == 200
    assert policy_client.post(
        f"/api/oracle/proposals/{pid}/revoke",
        headers={"X-Operator-Token": "ci-local-only-secret"}
    ).status_code == 403
    assert policy_client.post(
        f"/api/oracle/proposals/{pid}/revoke",
        headers={"X-Reviewer-Token": "separate-reviewer-secret"}
    ).status_code == 200



def test_candidate_dsl_rejects_untrusted_predicate(policy_client):
    rejected = policy_client.post("/api/oracle/proposals", json={
        "proposed_by": "model",
        "candidate": {
            "predicate": "evaluate-python",
            "source_ref": "arbitrary-website",
            "rationale": "This text requests a rule outside the fixed template",
        },
    })
    assert rejected.status_code == 422
    accepted = policy_client.post("/api/oracle/proposals", json={
        "proposed_by": "model",
        "candidate": {
            "rationale": "Initial stock and all confirmations must conserve stock",
        },
    })
    assert accepted.status_code == 201
    assert accepted.json()["evidence"]["verified"]
    assert accepted.json()["evidence"]["scope"] == "SHIPPED_SYNTHETIC_FIXTURE_ONLY"



def test_feedback_route_requires_operator_and_preserves_missing_evidence(policy_client):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "route-planner"}
    ).json()
    header = {"X-Operator-Token": "ci-local-only-secret"}
    policy_client.post(
        f"/api/oracle/proposals/{proposal['id']}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "reviewer"},
        headers=header,
    )
    created = policy_client.post(
        "/api/agent/plans", json={
            "proposal_id": proposal["id"], "strategy": "semantic",
            "variant": "racy", "seed": 0,
        },
    ).json()
    plan_id = created["id"]
    url = f"/api/agent/plans/{plan_id}/feedback"
    assert policy_client.post(url, json={"remaining_budget": 8}).status_code == 403
    assert policy_client.post(
        url, json={"remaining_budget": 8}, headers=header,
    ).status_code == 409
    assert policy_client.get(f"/api/agent/plans/{plan_id}/gaps").json()["gaps"] == []



def test_frontier_proposal_is_operator_gated(policy_client):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "frontier-proposer"}
    ).json()
    pid = proposal["id"]
    url = f"/api/agent/frontier/{pid}"
    assert policy_client.get(url).status_code == 409
    headers = {"X-Operator-Token": "ci-local-only-secret"}
    policy_client.post(
        f"/api/oracle/proposals/{pid}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "reviewer"},
        headers=headers,
    )
    result = policy_client.get(url).json()
    assert result["promotion_eligible"] is False
    body = {"proposal_id": pid, "variant": "racy"}
    assert policy_client.post(f"{url}/plans", json=body).status_code == 403
    planned = policy_client.post(f"{url}/plans", json=body, headers=headers)
    assert planned.status_code == 201
    assert planned.json()["plan"]["max_steps"] == 4



def test_budget_endpoint_is_operator_only(policy_client):
    assert policy_client.get("/api/agent/model-budget").status_code == 403
    response = policy_client.get(
        "/api/agent/model-budget",
        headers={"X-Operator-Token": "ci-local-only-secret"},
    )
    assert response.status_code == 200
    assert response.json()["token_limit"] >= 256


def test_private_reviewer_is_independent_of_operator_session(
    policy_client, monkeypatch,
):
    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "different-author"}
    ).json()
    pid = proposal["id"]
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "private")
    monkeypatch.setenv("CLASHBOLT_REVIEWER_TOKEN", "independent-review-only")
    approval = {"expected_hash": ORACLE_HASH, "reviewer": "separate-human"}
    url = f"/api/oracle/proposals/{pid}/approve"
    assert policy_client.post(url, json=approval).status_code == 403
    assert policy_client.post(
        url, json=approval, headers={"X-Operator-Token": "ci-local-only-secret"}
    ).status_code == 403
    response = policy_client.post(
        url, json=approval, headers={"X-Reviewer-Token": "independent-review-only"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "APPROVED"
    assert policy_client.get("/api/oracle/proposals").status_code == 401
    revoke = policy_client.post(
        f"/api/oracle/proposals/{pid}/revoke",
        headers={"X-Reviewer-Token": "independent-review-only"},
    )
    assert revoke.status_code == 200
    assert revoke.json()["state"] == "REVOKED"


def test_private_mode_scoped_tool_dispatch_is_not_operator_privilege(
    policy_client, monkeypatch,
):
    from fastapi.testclient import TestClient

    from clashbolt.api import create_app

    proposal = policy_client.post(
        "/api/oracle/proposals", json={"proposed_by": "planner"}
    ).json()
    rule_id = proposal["id"]
    operator_headers = {"X-Operator-Token": "ci-local-only-secret"}
    assert policy_client.post(
        f"/api/oracle/proposals/{rule_id}/approve",
        json={"expected_hash": ORACLE_HASH, "reviewer": "reviewer"},
        headers=operator_headers,
    ).status_code == 200
    planned = policy_client.post(
        "/api/agent/plans",
        json={
            "proposal_id": rule_id, "strategy": "semantic",
            "variant": "racy", "seed": 0,
        },
    ).json()
    plan_id = planned["id"]
    issued = policy_client.post(
        "/api/tool-capabilities",
        json={
            "proposal_id": rule_id, "variant": "racy",
            "schedule": "overlap", "plan_id": plan_id,
        },
        headers=operator_headers,
    )
    assert issued.status_code == 201
    token = issued.json()["token"]
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "private")
    with TestClient(create_app()) as guest:
        requested = {
            "variant": "racy", "schedule": "overlap", "plan_id": plan_id,
        }
        assert guest.get("/api/oracle/proposals").status_code == 401
        assert guest.post(
            "/api/agent/tools/run", json=requested,
        ).status_code == 403
        rejected = guest.post(
            "/api/agent/tools/run",
            json={**requested, "schedule": "serial"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rejected.status_code == 403
        result = guest.post(
            "/api/agent/tools/run", json=requested,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["verdict"] == "FAIL"
        assert guest.post(
            "/api/agent/tools/run", json=requested,
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 403
