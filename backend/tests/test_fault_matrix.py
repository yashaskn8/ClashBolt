"""Deterministic fault matrix for claim generations and terminal truth."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy


@pytest.fixture
def ledger():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI database required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposed = store.propose("fenced-crash-matrix")
    approved = store.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    return store, UUID(approved["id"])


def test_120_deterministic_claim_cancel_and_duplicate_receipts(ledger):
    """No real host outage is simulated; lease and result boundaries are tested."""
    store, approved = ledger
    witness = asyncio.run(execute("locked", "serial"))
    outcomes = {"COMPLETE": 0, "UNKNOWN": 0}
    for index in range(120):
        submitted = store.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=approved,
        )
        run_id = UUID(submitted["run_id"])
        owner = f"crash-injection-{index}"
        first = store.claim(owner, lease_seconds=5)
        assert first and first["run_id"] == submitted["run_id"]
        generation = first["lease_generation"]

        if index % 3 == 0:
            # Expire a worker claim and reject its late result after takeover.
            with store.connect() as conn:
                conn.execute(
                    "UPDATE experiment_run SET lease_until = now() - interval "
                    "'1 second' WHERE id = %s",
                    (run_id,),
                )
            second_owner = f"{owner}-replacement"
            second = store.claim(second_owner, lease_seconds=5)
            assert second and second["run_id"] == submitted["run_id"]
            assert not store.finish(run_id, owner, generation, witness)
            assert store.finish(
                run_id, second_owner, second["lease_generation"], witness
            )
            expected = ["PENDING", "RUNNING", "RUNNING", "COMPLETE"]
        elif index % 3 == 1:
            # Cancellation cannot promise to roll back a target side effect.
            assert store.cancel(run_id)["status"] == "CANCELLING"
            with store.connect() as conn:
                conn.execute(
                    "UPDATE experiment_run SET lease_until = now() - interval "
                    "'1 second' WHERE id = %s",
                    (run_id,),
                )
            assert store.reconcile_expired_cancelled() == 1
            assert not store.finish(run_id, owner, generation, witness)
            expected = ["PENDING", "RUNNING", "CANCELLING", "UNKNOWN"]
        else:
            assert store.finish(run_id, owner, generation, witness)
            assert not store.finish(run_id, owner, generation, witness)
            expected = ["PENDING", "RUNNING", "COMPLETE"]

        final = store.get(run_id)
        outcomes[final["status"]] += 1
        assert [e["status"] for e in store.transitions(run_id)] == expected
        assert final["verdict"] == (
            "UNKNOWN" if final["status"] == "UNKNOWN" else "PASS"
        )
    assert outcomes == {"COMPLETE": 80, "UNKNOWN": 40}
