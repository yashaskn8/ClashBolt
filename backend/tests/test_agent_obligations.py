"""A5 durable agent obligation: versioned question, dispatch and receipt.

Only the shipped synthetic tool is in scope. These tests establish database
control-plane recovery, not external exactly-once target effects.
"""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.planner import choose_plan
from clashbolt.policy import CONTEXT_VERSION, TOOL_SCHEMA_VERSION, Policy
from fastapi.testclient import TestClient


@pytest.fixture
def policy():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL is needed for durable agent obligations")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    return store


def _ready(policy, schedule="overlap"):
    proposed = policy.propose("agent-obligation-test")
    oracle_id = UUID(proposed["id"])
    policy.approve(oracle_id, ORACLE_HASH, "independent-reviewer")
    plan = policy.record_plan(
        oracle_id, choose_plan(strategy="semantic", variant="racy")
        if schedule == "overlap" else {
            **choose_plan(strategy="semantic", variant="racy"),
            "schedule": "serial",
        }
    )
    return oracle_id, UUID(plan["id"])


def test_question_and_tool_receipt_survive_new_store_instance(policy, monkeypatch):
    oracle_id, plan_id = _ready(policy)
    proposed = policy.get_plan(plan_id)
    question = proposed["obligation"]
    assert question["context_version"] == CONTEXT_VERSION
    assert question["tool_schema_version"] == TOOL_SCHEMA_VERSION
    assert question["model_identifier"] == "DETERMINISTIC_BASELINE"
    assert question["state"] == "PENDING"
    assert question["tool_request_id"] is None
    assert policy.plan_history(plan_id)["integrity"]["status"] == "VERIFIED"

    token = policy.issue(oracle_id, "racy", "overlap", plan_id=plan_id)["token"]
    assert policy.authorize(token, "racy", "overlap", plan_id)
    fresh = Policy(policy.dsn)
    dispatched = fresh.get_plan(plan_id)["obligation"]
    assert dispatched["question_id"] == question["question_id"]
    assert dispatched["state"] == "IN_FLIGHT"
    assert UUID(dispatched["tool_request_id"])
    assert fresh.plan_history(plan_id)["integrity"]["status"] == "VERIFIED"

    assert fresh.record_outcome(plan_id, asyncio.run(execute("racy", "overlap")))
    receipt = policy.get_plan(plan_id)["obligation"]
    assert receipt["state"] == "FULFILLED"
    assert UUID(receipt["receipt_id"])
    assert receipt["tool_request_id"] == dispatched["tool_request_id"]
    history = policy.plan_history(plan_id)
    assert history["integrity"]["status"] == "VERIFIED"
    assert history["obligation"]["receipt_id"] == receipt["receipt_id"]
    assert history["entries"][0]["payload"]["obligation"]["question_id"] == (
        question["question_id"]
    )
    assert history["entries"][1]["payload"]["tool_request_id"] == (
        receipt["tool_request_id"]
    )
    assert history["entries"][2]["payload"]["receipt_id"] == receipt["receipt_id"]

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", policy.dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "a5-history-access")
    with TestClient(create_app()) as client:
        response = client.get(f"/api/agent/plans/{plan_id}")
        assert response.status_code == 200
        assert response.json()["obligation"]["state"] == "FULFILLED"
        assert client.get(f"/api/agent/plans/{plan_id}/history").status_code == 403
        authorized = client.get(
            f"/api/agent/plans/{plan_id}/history",
            headers={"X-Operator-Token": "a5-history-access"},
        )
        assert authorized.status_code == 200
        assert authorized.json()["integrity"]["status"] == "VERIFIED"


def test_changed_question_or_model_identity_denies_tool_and_quarantines(policy):
    oracle_id, plan_id = _ready(policy)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_obligation SET model_identifier = 'forged-model' "
            "WHERE plan_id = %s", (plan_id,),
        )
    assert policy.plan_history(plan_id)["integrity"]["status"] == "INVALID"
    with pytest.raises(ValueError, match="pending approved plan"):
        policy.issue(oracle_id, "racy", "overlap", plan_id=plan_id)
    assert policy.reconcile_corrupt_plans() == 1
    assert policy.get_plan(plan_id)["obligation"]["state"] == "UNKNOWN"
    assert policy.get_plan(plan_id)["result"]["quarantined"] is True


def test_replaced_request_id_cannot_be_used_to_certify_receipt(policy):
    oracle_id, plan_id = _ready(policy)
    token = policy.issue(oracle_id, "racy", "overlap", plan_id=plan_id)["token"]
    assert policy.authorize(token, "racy", "overlap", plan_id)
    original = policy.get_plan(plan_id)["obligation"]["tool_request_id"]
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_obligation SET tool_request_id = %s "
            "WHERE plan_id = %s", (uuid4(), plan_id),
        )
    assert policy.plan_history(plan_id)["integrity"]["status"] == "INVALID"
    assert policy.record_outcome(plan_id, asyncio.run(execute("racy", "overlap")))
    current = policy.get_plan(plan_id)
    assert current["state"] == "UNKNOWN"
    assert current["result"]["verdict"] == "UNKNOWN"
    assert current["result"]["evidence_sha256"] is None
    assert current["obligation"]["tool_request_id"] != original


def test_timeout_keeps_unresolved_request_with_no_phantom_receipt(policy):
    oracle_id, plan_id = _ready(policy)
    token = policy.issue(oracle_id, "racy", "overlap", plan_id=plan_id)["token"]
    assert policy.authorize(token, "racy", "overlap", plan_id)
    requested = policy.get_plan(plan_id)["obligation"]["tool_request_id"]
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET deadline_at = now() - interval '1 second' "
            "WHERE id = %s", (plan_id,),
        )
    restarted = Policy(policy.dsn)
    assert restarted.reconcile_stalled_plans() == 1
    assert restarted.reconcile_stalled_plans() == 0
    obligation = restarted.get_plan(plan_id)["obligation"]
    assert obligation["state"] == "UNKNOWN"
    assert obligation["tool_request_id"] == requested
    assert obligation["receipt_id"] is None
    history = restarted.plan_history(plan_id)
    assert history["integrity"]["status"] == "VERIFIED"
    assert history["entries"][-1]["event"] == "TIMEOUT"
    assert history["entries"][-1]["payload"]["tool_request_id"] == requested
    assert not restarted.record_outcome(
        plan_id, asyncio.run(execute("racy", "overlap"))
    )


def test_oracle_revocation_withdraws_fulfilled_obligation(policy):
    oracle_id, plan_id = _ready(policy)
    token = policy.issue(oracle_id, "racy", "overlap", plan_id=plan_id)["token"]
    assert policy.authorize(token, "racy", "overlap", plan_id)
    assert policy.record_outcome(plan_id, asyncio.run(execute("racy", "overlap")))
    completed = policy.get_plan(plan_id)
    assert completed["state"] == "COMPLETE"
    assert policy.revoke_rule(oracle_id)
    withdrawn = policy.get_plan(plan_id)
    assert withdrawn["state"] == "UNKNOWN"
    assert withdrawn["obligation"]["state"] == "UNKNOWN"
    assert withdrawn["obligation"]["receipt_id"] == (
        completed["obligation"]["receipt_id"]
    )
    assert withdrawn["result"]["evidence_sha256"] is None
    assert policy.plan_history(plan_id)["integrity"]["status"] == "VERIFIED"
    assert policy.record_feedback(plan_id, remaining_budget=4)["decision"]["action"] == (
        "ABSTAIN"
    )


def test_feedback_creates_new_unprivileged_versioned_question(policy):
    oracle_id, plan_id = _ready(policy, schedule="serial")
    token = policy.issue(oracle_id, "racy", "serial", plan_id=plan_id)["token"]
    assert policy.authorize(token, "racy", "serial", plan_id)
    assert policy.record_outcome(plan_id, asyncio.run(execute("racy", "serial")))
    feedback = policy.record_feedback(plan_id, remaining_budget=4)
    assert feedback["decision"]["action"] == "REPLAN"
    next_plan = Policy(policy.dsn).get_plan(UUID(feedback["next_plan_id"]))
    assert next_plan["state"] == "PROPOSED"
    assert next_plan["obligation"]["state"] == "PENDING"
    assert next_plan["obligation"]["question_id"] != (
        policy.get_plan(plan_id)["obligation"]["question_id"]
    )
    assert policy.plan_history(UUID(next_plan["id"]))["integrity"]["status"] == (
        "VERIFIED"
    )
