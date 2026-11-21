"""Deterministic fault matrix for claim generations and terminal truth."""

import asyncio
import json
import os
import select
import subprocess
import sys
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX process termination test")
def test_real_claim_owner_process_crash_is_reclaimed_without_duplicate_verdict(ledger):
    """Kill a claimant OS process, then let a new generation finish the run."""
    store, approved = ledger
    run = store.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=approved,
    )
    run_id = UUID(run["run_id"])
    script = (
        "import json, os, time\n"
        "from clashbolt.store import Ledger\n"
        "claim = Ledger(os.environ['CLASHBOLT_TEST_DATABASE_URL']).claim("
        "'crashed-process', lease_seconds=5)\n"
        "print(json.dumps(claim), flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=os.environ.copy(),
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, "Child did not report a durable claim"
        claimed = json.loads(child.stdout.readline())
        assert claimed["run_id"] == run["run_id"]
        first_generation = claimed["lease_generation"]
        assert store.get(run_id)["status"] == "RUNNING"
    finally:
        child.kill()
        child.communicate(timeout=5)
    assert child.returncode is not None and child.returncode != 0

    # Move database time past the lease without waiting; the actual claimant
    # is dead, while the remaining recovery steps use normal coordinator APIs.
    with store.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
            "WHERE id = %s", (run_id,),
        )
    recovered = store.claim("replacement-process", lease_seconds=30)
    assert recovered and recovered["run_id"] == run["run_id"]
    assert recovered["lease_generation"] == first_generation + 1
    witness = asyncio.run(execute("locked", "serial"))
    assert not store.finish(run_id, "crashed-process", first_generation, witness)
    assert store.finish(
        run_id, "replacement-process", recovered["lease_generation"], witness
    )
    assert not store.finish(
        run_id, "replacement-process", recovered["lease_generation"], witness
    )
    assert store.get(run_id)["verdict"] == "PASS"
    assert [row["status"] for row in store.transitions(run_id)] == [
        "PENDING", "RUNNING", "RUNNING", "COMPLETE",
    ]
