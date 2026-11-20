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


def test_expired_tool_plan_abstains_and_cannot_late_certify(policy):
    import asyncio
    from uuid import UUID

    from clashbolt.experiment import execute

    proposal = policy.propose()
    pid = UUID(proposal["id"])
    policy.approve(pid, ORACLE_HASH, "operator")
    plan = policy.record_plan(pid, choose_plan(strategy="semantic", variant="racy"))
    planned_id = UUID(plan["id"])
    cap = policy.issue(pid, "racy", "overlap", plan_id=planned_id)
    assert policy.authorize(cap["token"], "racy", "overlap", planned_id)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET deadline_at = now() - interval '1 second' "
            "WHERE id = %s", (planned_id,),
        )
    assert policy.reconcile_stalled_plans() == 1
    saved = policy.get_plan(planned_id)
    assert saved["state"] == "UNKNOWN"
    assert saved["result"]["verdict"] == "UNKNOWN"
    assert "missing" in saved["result"]["evidence_gap"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert not policy.record_outcome(planned_id, witness)


def test_incomplete_and_revoked_oracle_cannot_complete_plan(policy):
    from uuid import UUID

    proposal = policy.propose()
    pid = UUID(proposal["id"])
    policy.approve(pid, ORACLE_HASH, "operator")
    plan = policy.record_plan(pid, choose_plan(strategy="semantic", variant="racy"))
    plan_id = UUID(plan["id"])
    cap = policy.issue(pid, "racy", "overlap", plan_id=plan_id)
    assert policy.authorize(cap["token"], "racy", "overlap", plan_id)
    assert policy.revoke_rule(pid)
    incomplete = {"variant": "racy", "schedule": "overlap", "complete": False}
    assert policy.record_outcome(plan_id, incomplete)
    stored = policy.get_plan(plan_id)
    assert stored["state"] == "UNKNOWN"
    assert stored["result"]["verdict"] == "UNKNOWN"


def test_evaluation_manifest_is_frozen_and_cannot_promote_synthetic_only():
    import asyncio

    from clashbolt.planner import compare_strategies

    first = asyncio.run(compare_strategies(4))
    second = asyncio.run(compare_strategies(4))
    assert first == second
    assert not first["manifest"]["held_out"]
    assert not first["promotion_eligible"]
    assert len(first["manifest_sha256"]) == 64
    for strategy in first["strategies"].values():
        assert strategy["gate_budget"] == 16
        assert strategy["trials"] == 4
        assert len(strategy["receipts"]) == 4



def test_proposal_includes_validated_candidate_evidence(policy):
    from uuid import UUID

    from clashbolt.oracle_workshop import RuleCandidate

    created = policy.propose("candidate-lab", RuleCandidate(
        rationale="A confirmation cannot exceed initial reserved stock."
    ))
    evidence = created["evidence"]
    assert evidence["verified"]
    assert [case["actual"] for case in evidence["test_vectors"]] == [
        "PASS", "FAIL", "UNKNOWN"
    ]
    with policy.connect() as conn:
        conn.execute(
            "UPDATE oracle_proposal SET evidence = jsonb_set(evidence, "
            "'{verified}', 'false'::jsonb) WHERE id = %s",
            (UUID(created["id"]),),
        )
    with pytest.raises(ValueError, match="Stale"):
        policy.approve(UUID(created["id"]), ORACLE_HASH, "independent-reviewer")



def test_persisted_feedback_replans_only_once_and_without_tool_authority(policy):
    import asyncio
    from uuid import UUID

    from clashbolt.experiment import execute

    proposal = policy.propose("feedback-source")
    proposal_id = UUID(proposal["id"])
    policy.approve(proposal_id, ORACLE_HASH, "reviewer")
    from clashbolt.planner import choose_plan
    from psycopg.types.json import Jsonb

    prior = policy.record_plan(
        proposal_id, choose_plan(strategy="random", variant="racy", seed=0)
    )
    prior_id = UUID(prior["id"])
    assert prior["plan"]["schedule"] == "serial" or prior["plan"]["schedule"] == "overlap"
    # Use explicit serial baseline to create a known, complete PASS.
    serial_plan = dict(prior["plan"], schedule="serial")
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET plan = %s WHERE id = %s",
            (Jsonb(serial_plan), prior_id),
        )
    grant = policy.issue(
        proposal_id, "racy", "serial", plan_id=prior_id
    )
    assert policy.authorize(grant["token"], "racy", "serial", prior_id)
    witness = asyncio.run(execute("racy", "serial"))
    assert policy.record_outcome(prior_id, witness)
    first = policy.record_feedback(prior_id, 4)
    again = policy.record_feedback(prior_id, 4)
    assert first == again
    assert first["decision"]["action"] == "REPLAN"
    assert first["decision"]["automatically_authorized"] is False
    assert first["next_plan_id"] is not None
    next_plan = policy.get_plan(UUID(first["next_plan_id"]))
    assert next_plan["state"] == "PROPOSED"
    assert next_plan["plan"]["schedule"] == "overlap"
    assert policy.get_feedback(prior_id) == first
    with pytest.raises(ValueError, match="budget changed"):
        policy.record_feedback(prior_id, 8)


def test_abstention_after_expired_receipt_persists_gap(policy):
    from uuid import UUID

    proposal = policy.propose("gap-lab")
    proposal_id = UUID(proposal["id"])
    policy.approve(proposal_id, ORACLE_HASH, "reviewer")
    plan = policy.record_plan(
        proposal_id, choose_plan(strategy="semantic", variant="racy")
    )
    plan_id = UUID(plan["id"])
    capability = policy.issue(proposal_id, "racy", "overlap", plan_id=plan_id)
    assert policy.authorize(capability["token"], "racy", "overlap", plan_id)
    with policy.connect() as conn:
        conn.execute(
            "UPDATE agent_plan SET deadline_at = now() - interval '1 second' "
            "WHERE id = %s", (plan_id,),
        )
    assert policy.reconcile_stalled_plans() == 1
    gaps = policy.evidence_gaps(plan_id)
    assert len(gaps) == 1
    assert gaps[0]["classification"] == "MISSING_RECEIPT"
    feedback = policy.record_feedback(plan_id, 8)
    assert feedback["decision"]["action"] == "ABSTAIN"
    assert feedback["next_plan_id"] is None



def test_frontier_scopes_to_approved_rule_and_stored_receipts(policy):
    from uuid import UUID

    proposal = policy.propose("frontier-source")
    proposal_id = UUID(proposal["id"])
    with pytest.raises(ValueError, match="approved"):
        policy.frontier(proposal_id, "racy")
    policy.approve(proposal_id, ORACLE_HASH, "independent-reviewer")
    result = policy.frontier(proposal_id, "racy")
    assert result["scope"] == "SHIPPED_SYNTHETIC_FIXTURE_ONLY"
    assert [item["schedule"] for item in result["candidates"]] == [
        "overlap", "serial"
    ]
    proposal_plan = policy.propose_frontier_plan(proposal_id, "racy")
    assert proposal_plan["plan"]["planner_kind"] == "DETERMINISTIC_EVIDENCE_FRONTIER"
    assert proposal_plan["plan"]["schedule"] == "overlap"
    assert proposal_plan["state"] == "PROPOSED"
    assert policy.revoke_rule(proposal_id)
    with pytest.raises(ValueError, match="approved"):
        policy.propose_frontier_plan(proposal_id, "racy")



def test_model_token_reservations_are_atomic_and_conservative(policy):
    with policy.connect() as conn:
        conn.execute("DELETE FROM model_reservation")
        conn.execute("DELETE FROM model_budget")
        conn.execute("INSERT INTO project_quota(project_id) VALUES ('local') "
                     "ON CONFLICT DO NOTHING")
        conn.execute(
            """
            INSERT INTO model_budget(project_id, window_date, call_limit, token_limit)
            VALUES ('local', CURRENT_DATE, 1, 256)
            """
        )
    with ThreadPoolExecutor(max_workers=6) as pool:
        def attempt(_):
            try:
                return policy.reserve_model_call()
            except ValueError:
                return None

        accepted = list(pool.map(attempt, range(6)))
    reservations = [x for x in accepted if x is not None]
    assert len(reservations) == 1
    budget = policy.local_model_budget()
    assert budget["calls_reserved"] == 1
    assert budget["tokens_reserved"] == 256
    assert policy.settle_model_call(reservations[0], None)
    assert not policy.settle_model_call(reservations[0], 10)
    assert policy.local_model_budget()["tokens_observed"] == 0



def test_approved_oracle_is_database_immutable_except_for_revocation(policy):
    from uuid import UUID

    import psycopg

    proposed = policy.propose("immutable-oracle")
    oracle_id = UUID(proposed["id"])
    policy.approve(oracle_id, ORACLE_HASH, "reviewer")
    with pytest.raises(psycopg.errors.RaiseException, match="identity"):
        with policy.connect() as conn:
            conn.execute(
                "UPDATE oracle_proposal SET rule_hash = 'not-approved' WHERE id = %s",
                (oracle_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="only be revoked"):
        with policy.connect() as conn:
            conn.execute(
                "UPDATE oracle_proposal SET reviewed_by = 'spoofed' WHERE id = %s",
                (oracle_id,),
            )
    assert policy.revoke_rule(oracle_id)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with policy.connect() as conn:
            conn.execute(
                "UPDATE oracle_proposal SET state = 'APPROVED' WHERE id = %s",
                (oracle_id,),
            )
