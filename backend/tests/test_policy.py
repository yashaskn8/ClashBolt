import os
from concurrent.futures import ThreadPoolExecutor
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


def test_concurrent_one_use_tool_authorizes_once(policy):
    from uuid import UUID

    proposal = policy.propose()
    pid = UUID(proposal["id"])
    policy.approve(pid, ORACLE_HASH, "operator")
    capability = policy.issue(pid, "racy", "overlap", max_calls=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(
            pool.map(lambda _: policy.authorize(
                capability["token"], "racy", "overlap"
            ), range(8))
        )
    assert sum(decisions) == 1
    assert len(policy.audit()) == 8


def test_plan_binding_and_receipt(policy):
    import asyncio
    from uuid import UUID

    from clashbolt.experiment import execute

    proposal = policy.propose()
    pid = UUID(proposal["id"])
    policy.approve(pid, ORACLE_HASH, "operator")
    plan = policy.record_plan(
        pid, choose_plan(strategy="semantic", variant="racy")
    )
    planned_id = UUID(plan["id"])
    grant = policy.issue(pid, "racy", "overlap", plan_id=planned_id)
    assert not policy.authorize(grant["token"], "racy", "overlap")
    assert policy.authorize(grant["token"], "racy", "overlap", planned_id)
    assert not policy.authorize(grant["token"], "racy", "overlap", planned_id)
    witness = asyncio.run(execute("racy", "overlap"))
    assert policy.record_outcome(planned_id, witness)
    assert not policy.record_outcome(planned_id, witness)
    stored = policy.get_plan(planned_id)
    assert stored["state"] == "COMPLETE"
    assert stored["result"]["verdict"] == "FAIL"
