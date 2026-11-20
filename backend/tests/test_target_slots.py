"""Native target slot accounting is serialized without starving memory work."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH
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
    proposal = store.propose("native-slot-test")
    approved = store.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    return store, UUID(approved["id"])


def test_concurrent_native_slots_are_capped_but_memory_can_pass(
    ledger, monkeypatch,
):
    store, approved = ledger
    target = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not target:
        pytest.skip("Separate native test cluster required")
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", target)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO project_quota(project_id, max_native_active)
            VALUES ('local', 1)
            ON CONFLICT (project_id)
            DO UPDATE SET max_native_active = EXCLUDED.max_native_active
            """
        )

    def admit(backend):
        return store.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            execution_backend=backend, approved_oracle_id=approved,
        )

    first = admit("native-postgresql")
    blocked = admit("native-postgresql")
    memory = admit("memory")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(
            lambda index: store.claim(f"slot-worker-{index}"), range(4)
        ))
    accepted = [item for item in claims if item]
    assert {item["run_id"] for item in accepted} == {
        first["run_id"], memory["run_id"],
    }
    assert store.get(UUID(blocked["run_id"]))["status"] == "PENDING"
    assert store.operations()["native_slots_occupied"] == 1
    assert store.operations()["native_slots_limit"] == 1
    assert store.claim("no-available-slot") is None

    assert store.cancel(UUID(first["run_id"]))["status"] == "CANCELLING"
    assert store.claim("cancellation-still-occupies-slot") is None
    with store.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval "
            "'1 second' WHERE id = %s",
            (UUID(first["run_id"]),),
        )
    assert store.reconcile_expired_cancelled() == 1
    next_claim = store.claim("slot-recovery")
    assert next_claim and next_claim["run_id"] == blocked["run_id"]
    assert store.operations()["native_slots_occupied"] == 1
