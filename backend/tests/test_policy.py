import os
from uuid import uuid4

import pytest

from clashbolt.experiment import ORACLE_HASH
from clashbolt.planner import choose_plan, propose_follow_up
from clashbolt.policy import Policy


@pytest.fixture
def policy():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database not configured")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    return store


def test_exact_version_review_and_self_approval_denied(policy):
    proposed = policy.propose("planner")
    with pytest.raises(ValueError, match="Stale"):
        policy.approve(uuid4(), ORACLE_HASH, "reviewer")
    with pytest.raises(ValueError, match="Stale"):
        policy.approve(uuid4(), "bad-hash", "reviewer")
    from uuid import UUID

    with pytest.raises(ValueError, match="proposer"):
        policy.approve(UUID(proposed["id"]), ORACLE_HASH, "planner")
    approved = policy.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    assert approved["state"] == "APPROVED"
    with pytest.raises(ValueError, match="Stale"):
        policy.approve(UUID(proposed["id"]), ORACLE_HASH, "another")


def test_scoped_capability_budget_and_revoke(policy):
    from uuid import UUID

    proposed = policy.propose()
    pid = UUID(proposed["id"])
    with pytest.raises(ValueError, match="approved"):
        policy.issue(pid, "racy", "overlap")
    policy.approve(pid, ORACLE_HASH, "operator")
    capability = policy.issue(pid, "racy", "overlap", max_calls=2)
    token = capability["token"]
    assert not policy.authorize(token, "racy", "serial")
    assert policy.authorize(token, "racy", "overlap")
    assert policy.authorize(token, "racy", "overlap")
    assert not policy.authorize(token, "racy", "overlap")
    assert policy.audit()[0]["decision"] == "DENY"

    second = policy.issue(pid, "racy", "serial")
    assert policy.revoke_capability(UUID(second["capability_id"]))
    assert not policy.authorize(second["token"], "racy", "serial")
    third = policy.issue(pid, "locked", "overlap")
    assert policy.revoke_rule(pid)
    assert not policy.authorize(third["token"], "locked", "overlap")


def test_persist_typed_plans_with_approved_rule(policy):
    from uuid import UUID

    proposal = policy.propose()
    pid = UUID(proposal["id"])
    plan = choose_plan(strategy="semantic", variant="racy")
    with pytest.raises(ValueError, match="approved"):
        policy.record_plan(pid, plan)
    policy.approve(pid, ORACLE_HASH, "operator")
    recorded = policy.record_plan(pid, plan)
    assert recorded["plan"]["schedule"] == "overlap"
    assert recorded["state"] == "PROPOSED"


def test_planner_abstention_and_conflict_replan():
    plan = choose_plan(strategy="semantic", variant="racy", seed=10)
    assert plan["schedule"] == "overlap"
    assert propose_follow_up({"verdict": "UNKNOWN"}, budget_remaining=8)["action"] == "ABSTAIN"
    assert propose_follow_up(
        {"verdict": "PASS", "variant": "racy", "schedule": "serial"},
        budget_remaining=8,
    )["action"] == "REPLAN"
    assert propose_follow_up({"verdict": "FAIL"}, budget_remaining=8)["action"] == "STOP"
