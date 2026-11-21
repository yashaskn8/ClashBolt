"""Cross-project PostgreSQL claim locks enforce one shared native target cap."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.store import GLOBAL_NATIVE_SLOTS
from clashbolt.verification_jobs import VerificationJobs


def test_native_run_and_verification_claims_share_global_project_capacity(
    monkeypatch,
):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    target = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not dsn or not target:
        pytest.skip("Independent native PostgreSQL test clusters required")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", target)
    registry = AccountStore(dsn)
    registry.migrate()
    stores = []
    admitted = []
    for index in range(GLOBAL_NATIVE_SLOTS + 1):
        project_id = f"capacity-{uuid4().hex[:12]}"
        schema = registry.create_project(project_id)
        policy = Policy(dsn, schema=schema, project_id=project_id)
        proposed = policy.propose(f"capacity-planner-{index}")
        oracle_id = UUID(proposed["id"])
        policy.approve(oracle_id, ORACLE_HASH, "independent-reviewer")
        stores.append((policy, oracle_id))

    # The verification queue consumes the same disposable target host.
    # Prepare a completed memory witness before admitting native work.
    verifier_policy, verifier_oracle = stores[0]
    memory_run = verifier_policy.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=verifier_oracle,
    )
    memory_claim = verifier_policy.claim("capacity-memory-worker")
    assert memory_claim and memory_claim["run_id"] == memory_run["run_id"]
    witness = asyncio.run(execute("locked", "serial"))
    assert verifier_policy.finish(
        UUID(memory_claim["run_id"]), "capacity-memory-worker",
        memory_claim["lease_generation"], witness,
    )
    verification = VerificationJobs(
        dsn, schema=verifier_policy.schema,
        project_id=verifier_policy.project_id,
    )
    submitted = verification.submit(UUID(memory_run["run_id"]), "REPLAY")
    assert submitted["status"] == "PENDING"

    for policy, oracle_id in stores:
        admitted.append(policy.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=oracle_id, execution_backend="native-postgresql",
        ))
    try:
        # These transactions race across distinct schema-local quota rows.
        # Only the *shared* advisory lock prevents overclaiming.
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            claims = list(pool.map(
                lambda item: item[1].claim(f"capacity-worker-{item[0]}"),
                enumerate(policy for policy, _ in stores),
            ))
        assert sum(claim is not None for claim in claims) == GLOBAL_NATIVE_SLOTS
        assert verification.claim("capacity-verifier") is None

        pending = [i for i, claim in enumerate(claims) if claim is None]
        assert len(pending) == 1
        blocked_index = pending[0]
        assert stores[blocked_index][0].get(
            UUID(admitted[blocked_index]["run_id"])
        )["status"] == "PENDING"

        # An eligible in-memory job must not starve behind a native slot cap.
        blocked = stores[blocked_index][0]
        fresh_memory = blocked.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=stores[blocked_index][1],
        )
        memory = blocked.claim("capacity-in-memory")
        assert memory and memory["run_id"] == fresh_memory["run_id"]
        assert blocked.finish(
            UUID(memory["run_id"]), "capacity-in-memory",
            memory["lease_generation"], witness,
        )

        # Once a native lease expires, a queued verification may reserve the
        # freed slot, even if another native job remains pending.
        active_index = next(i for i, claim in enumerate(claims) if claim)
        active = claims[active_index]
        with stores[active_index][0].connect() as conn:
            conn.execute(
                "UPDATE experiment_run SET lease_until = now() - interval '1 second'"
                " WHERE id = %s",
                (UUID(active["run_id"]),),
            )
        verified_claim = verification.claim("capacity-verifier")
        assert verified_claim is not None
        assert verified_claim["job_id"] == submitted["job_id"]
        assert blocked.claim("capacity-still-full") is None
    finally:
        # Keep the shared CI test database free of artificial active leases.
        for policy, _ in stores:
            with policy.connect() as conn:
                conn.execute(
                    "UPDATE experiment_run "
                    "SET lease_until = now() - interval '1 second' "
                    "WHERE lease_owner LIKE 'capacity-%'"
                )
                conn.execute(
                    "UPDATE verification_job "
                    "SET lease_until = now() - interval '1 second' "
                    "WHERE lease_owner LIKE 'capacity-%'"
                )
