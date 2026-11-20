"""PostgreSQL agent decision history and fail-closed integrity tests."""

import asyncio
import os
from uuid import UUID

import psycopg
import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.planner import choose_plan
from clashbolt.policy import Policy
from fastapi.testclient import TestClient


@pytest.fixture
def policy():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    return store


def _approved_plan(policy):
    proposed = policy.propose("decision-history-test")
    pid = UUID(proposed["id"])
    policy.approve(pid, ORACLE_HASH, "reviewer")
    plan = policy.record_plan(pid, choose_plan(strategy="semantic", variant="racy"))
    return pid, UUID(plan["id"])


def test_history_records_the_exact_approved_action_and_receipt(policy):
    proposal_id, plan_id = _approved_plan(policy)
    first = policy.plan_history(plan_id)
    assert first["integrity"]["status"] == "VERIFIED"
    assert [e["event"] for e in first["entries"]] == ["PROPOSED"]
    assert first["entries"][0]["payload"]["rule_sha256"] == ORACLE_HASH

    cap = policy.issue(proposal_id, "racy", "overlap", plan_id=plan_id)
    assert policy.authorize(cap["token"], "racy", "overlap", plan_id)
    assert policy.record_outcome(plan_id, asyncio.run(execute("racy", "overlap")))
    current = policy.plan_history(plan_id)
    assert [e["event"] for e in current["entries"]] == [
        "PROPOSED", "DISPATCHED", "RECEIPT"
    ]
    assert current["integrity"]["entries"] == 3
    assert current["integrity"]["status"] == "VERIFIED"
    assert current["entries"][-1]["payload"]["receipt"]["verdict"] == "FAIL"

    from clashbolt.agent_history import verify_history

    forged = [dict(item) for item in current["entries"]]
    forged[-1]["entry_sha256"] = "f" * 64
    assert verify_history(plan_id, forged)["status"] == "INVALID"

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with policy.connect() as conn:
            conn.execute(
                "UPDATE agent_history SET entry_sha256 = %s WHERE plan_id = %s",
                ("f" * 64, plan_id),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with policy.connect() as conn:
            conn.execute("DELETE FROM agent_history WHERE plan_id = %s", (plan_id,))
    assert policy.plan_history(plan_id)["integrity"]["status"] == "VERIFIED"


def test_mutated_plan_cannot_use_issued_capability(policy):
    pid, plan_id = _approved_plan(policy)
    cap = policy.issue(pid, "racy", "overlap", plan_id=plan_id)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET plan = jsonb_set(plan, '{schedule}', "
            "'\"serial\"'::jsonb) WHERE id = %s",
            (plan_id,),
        )
    assert policy.plan_history(plan_id)["integrity"]["status"] == "INVALID"
    assert not policy.authorize(cap["token"], "racy", "overlap", plan_id)
    assert policy.get_plan(plan_id)["state"] == "PROPOSED"


def test_mutated_inflight_plan_never_certifies_a_finding(policy):
    pid, plan_id = _approved_plan(policy)
    cap = policy.issue(pid, "racy", "overlap", plan_id=plan_id)
    assert policy.authorize(cap["token"], "racy", "overlap", plan_id)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET plan = jsonb_set(plan, '{schedule}', "
            "'\"serial\"'::jsonb) WHERE id = %s",
            (plan_id,),
        )
    assert policy.record_outcome(plan_id, asyncio.run(execute("racy", "overlap")))
    current = policy.get_plan(plan_id)
    assert current["state"] == "UNKNOWN"
    assert current["result"]["verdict"] == "UNKNOWN"
    assert "history" in current["result"]["evidence_gap"]


def test_timed_out_obligation_has_terminal_unknown_event(policy):
    pid, plan_id = _approved_plan(policy)
    cap = policy.issue(pid, "racy", "overlap", plan_id=plan_id)
    assert policy.authorize(cap["token"], "racy", "overlap", plan_id)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET deadline_at = now() - interval '1 second' "
            "WHERE id = %s", (plan_id,),
        )
    assert policy.reconcile_stalled_plans() == 1
    history = policy.plan_history(plan_id)
    assert [e["event"] for e in history["entries"]] == [
        "PROPOSED", "DISPATCHED", "TIMEOUT"
    ]
    assert history["integrity"]["status"] == "VERIFIED"
    assert history["current_state"] == "UNKNOWN"


def test_history_route_requires_operator_account_or_token(policy, monkeypatch):
    _, plan_id = _approved_plan(policy)
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", policy.dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "history-only-secret")
    with TestClient(create_app()) as client:
        url = f"/api/agent/plans/{plan_id}/history"
        assert client.get(url).status_code == 403
        permitted = client.get(
            url, headers={"X-Operator-Token": "history-only-secret"}
        )
        assert permitted.status_code == 200
        assert permitted.json()["integrity"]["status"] == "VERIFIED"
