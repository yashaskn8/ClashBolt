"""Concurrency regression: oracle withdrawal must fence new tool authorization."""

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from uuid import UUID

import pytest
from clashbolt.experiment import ORACLE_HASH
from clashbolt.planner import choose_plan
from clashbolt.policy import Policy


@pytest.fixture
def store():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database required")
    policy = Policy(dsn)
    policy.migrate()
    with policy.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    return policy


def _approved_rule(store):
    proposed = store.propose("revocation-fence-proposer")
    proposal_id = UUID(proposed["id"])
    store.approve(proposal_id, ORACLE_HASH, "reviewer")
    return proposal_id


def test_authorize_waits_for_oracle_revocation_and_denies_after_commit(store):
    rule_id = _approved_rule(store)
    cap = store.issue(rule_id, "racy", "overlap", max_calls=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connect() as conn:
            # This represents the revocation transaction holding the rule row.
            conn.execute(
                "SELECT id FROM oracle_proposal WHERE id = %s FOR UPDATE",
                (rule_id,),
            )
            future = pool.submit(store.authorize, cap["token"], "racy", "overlap")
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.2)
            conn.execute(
                "UPDATE oracle_proposal SET state = 'REVOKED' WHERE id = %s",
                (rule_id,),
            )
        assert future.result(timeout=5) is False
    with store.connect() as conn:
        count = conn.execute(
            "SELECT used_calls FROM tool_capability WHERE id = %s",
            (UUID(cap["capability_id"]),),
        ).fetchone()
    assert count["used_calls"] == 0
    assert store.audit()[0]["decision"] == "DENY"


def test_capability_issued_before_concurrent_revocation_cannot_dispatch(store):
    rule_id = _approved_rule(store)
    cap = store.issue(rule_id, "locked", "overlap")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connect() as conn:
            conn.execute(
                "SELECT id FROM tool_capability WHERE id = %s FOR UPDATE",
                (UUID(cap["capability_id"]),),
            )
            future = pool.submit(store.authorize, cap["token"], "locked", "overlap")
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.2)
            conn.execute(
                "UPDATE tool_capability SET revoked = TRUE WHERE id = %s",
                (UUID(cap["capability_id"]),),
            )
        assert future.result(timeout=5) is False
    assert store.audit()[0]["decision"] == "DENY"


def test_history_mutation_rejected_before_issuing_plan_capability(store):
    rule_id = _approved_rule(store)
    proposed = store.record_plan(
        rule_id, choose_plan(strategy="semantic", variant="racy")
    )
    plan_id = UUID(proposed["id"])
    with store.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET plan = jsonb_set(plan, '{schedule}', "
            "'\"serial\"'::jsonb) WHERE id = %s",
            (plan_id,),
        )
    with pytest.raises(ValueError, match="pending approved plan"):
        store.issue(rule_id, "racy", "serial", plan_id=plan_id)
    with store.connect() as conn:
        count = conn.execute(
            "SELECT count(*) AS count FROM tool_capability WHERE plan_id = %s",
            (plan_id,),
        ).fetchone()["count"]
    assert count == 0
