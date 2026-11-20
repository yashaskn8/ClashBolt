"""Model-call deadlines and crash reconciliation never create phantom authority."""

import os
from uuid import UUID

import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
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
        conn.execute("DELETE FROM model_reservation")
        conn.execute("DELETE FROM model_budget")
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal CASCADE"
        )
    return store


def test_expired_model_obligation_is_unknown_and_budget_never_refunded(policy):
    reservation_id = policy.reserve_model_call()
    before = policy.local_model_budget()
    assert before["calls_reserved"] == 1
    assert before["tokens_reserved"] == 256
    assert before["calls_inflight"] == 1
    with policy.connect() as conn:
        conn.execute(
            "UPDATE model_reservation SET deadline_at = now() - interval "
            "'1 second' WHERE id = %s", (reservation_id,),
        )
    assert policy.reconcile_stalled_model_calls() == 1
    assert policy.reconcile_stalled_model_calls() == 0
    assert policy.model_reservation_state(reservation_id) == "UNKNOWN"
    assert not policy.settle_model_call(reservation_id, 25)
    after = policy.local_model_budget()
    assert after["calls_unknown"] == 1
    assert after["calls_inflight"] == 0
    assert after["calls_reserved"] == before["calls_reserved"]
    assert after["tokens_reserved"] == before["tokens_reserved"]
    assert after["tokens_observed"] == 0


def test_timed_out_model_result_stays_unknown_even_when_tokens_are_known(policy):
    reservation_id = policy.reserve_model_call()
    with policy.connect() as conn:
        conn.execute(
            "UPDATE model_reservation SET deadline_at = now() - interval "
            "'1 second' WHERE id = %s", (reservation_id,),
        )
    assert policy.settle_model_call(reservation_id, 42)
    assert policy.model_reservation_state(reservation_id) == "UNKNOWN"
    assert policy.local_model_budget()["tokens_observed"] == 42
    assert not policy.settle_model_call(reservation_id, 42)
    assert policy.local_model_budget()["tokens_observed"] == 42
    with policy.connect() as conn:
        row = conn.execute(
            "SELECT finalized_at FROM model_reservation WHERE id = %s",
            (reservation_id,),
        ).fetchone()
        assert row["finalized_at"] is not None


def test_local_model_endpoint_rejects_a_late_valid_model_suggestion(
    policy, monkeypatch,
):
    proposed = policy.propose("model-deadline-test")
    proposal_id = UUID(proposed["id"])
    policy.approve(proposal_id, ORACLE_HASH, "reviewer")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", policy.dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "ci-model-deadline-secret")
    monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "test-model")

    async def model_suggestion(variant):
        with policy.connect() as conn:
            conn.execute(
                "UPDATE model_reservation SET deadline_at = "
                "now() - interval '1 second' WHERE state = 'RESERVED'"
            )
        return {
            **choose_plan(strategy="semantic", variant=variant),
            "strategy": "model",
            "planner_kind": "LOCAL_MODEL_UNPROMOTED",
            "model_status": "VALIDATED_PROPOSAL",
            "model_tokens_used": 12,
        }

    monkeypatch.setattr("clashbolt.routes_policy.propose_model_plan", model_suggestion)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/agent/plans/model",
            json={"proposal_id": str(proposal_id), "variant": "racy"},
            headers={"X-Operator-Token": "ci-model-deadline-secret"},
        )
        assert response.status_code == 201, response.text
        plan = response.json()["plan"]
        assert plan["strategy"] == "semantic"
        assert plan["planner_kind"] == "DETERMINISTIC_FALLBACK"
        assert plan["model_status"] == "LATE_OR_UNCONFIRMED_RESERVATION"
        assert policy.local_model_budget()["calls_unknown"] == 1
        assert policy.local_model_budget()["tokens_observed"] == 12


def test_reaper_is_safe_under_multiple_workers(policy):
    reservation_id = policy.reserve_model_call()
    with policy.connect() as conn:
        conn.execute(
            "UPDATE model_reservation SET deadline_at = now() - interval "
            "'1 second' WHERE id = %s", (reservation_id,),
        )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=5) as pool:
        counts = list(pool.map(lambda _: policy.reconcile_stalled_model_calls(), range(5)))
    assert sum(counts) == 1
    assert policy.model_reservation_state(reservation_id) == "UNKNOWN"
