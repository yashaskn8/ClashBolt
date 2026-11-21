"""Project native limits include verification jobs as well as native runs.

Exercises the real PostgreSQL quota-row and global-advisory-lock admission
paths; no target is provisioned and no external application is executed.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.verification_jobs import VerificationJobs


@pytest.fixture
def projects(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    target = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not dsn or not target:
        pytest.skip("Separate native PostgreSQL test clusters are required")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", target)
    registry = AccountStore(dsn)
    registry.migrate()
    stores = []

    def create():
        project_id = f"limit-{uuid4().hex[:12]}"
        schema = registry.create_project(project_id)
        policy = Policy(dsn, schema=schema, project_id=project_id)
        proposal = policy.propose("quota-fixture")
        approval = policy.approve(
            UUID(proposal["id"]), ORACLE_HASH, "independent-fixture-reviewer"
        )
        with policy.connect() as conn:
            conn.execute(
                "UPDATE project_quota SET max_native_active = 1 "
                "WHERE project_id = 'local'"
            )
        stores.append(policy)
        return policy, UUID(approval["id"])

    yield create

    for store in stores:
        with store.connect() as conn:
            # No targets are provisioned in these claim-only tests.
            conn.execute(
                "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
                "WHERE status IN ('RUNNING', 'CANCELLING')"
            )
            conn.execute(
                "UPDATE verification_job SET lease_until = now() - interval '1 second' "
                "WHERE status = 'RUNNING'"
            )


def _admit(policy, oracle_id, execution_backend="native-postgresql"):
    return policy.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=oracle_id, execution_backend=execution_backend,
    )


def test_local_cap_serializes_native_and_verification_claim_race(projects):
    policy, oracle_id = projects()
    memory = _admit(policy, oracle_id, "memory")
    initial = policy.claim("quota-seed")
    assert initial and initial["run_id"] == memory["run_id"]
    witness = asyncio.run(execute("locked", "serial"))
    assert policy.finish(
        UUID(memory["run_id"]), "quota-seed",
        initial["lease_generation"], witness,
    )
    queue = VerificationJobs(
        policy.dsn, schema=policy.schema, project_id=policy.project_id
    )
    queued = queue.submit(UUID(memory["run_id"]), "REPLAY")
    assert queued["status"] == "PENDING"
    native = _admit(policy, oracle_id)

    # Two separate connections race for the only project-native slot.
    with ThreadPoolExecutor(max_workers=2) as pool:
        run_future = pool.submit(policy.claim, "quota-native")
        check_future = pool.submit(queue.claim, "quota-verifier")
        run_claim = run_future.result(timeout=15)
        check_claim = check_future.result(timeout=15)
    assert (run_claim is None) != (check_claim is None)
    if run_claim:
        assert run_claim["run_id"] == native["run_id"]
        assert queue.claim("quota-verifier-blocked") is None
    else:
        assert check_claim["job_id"] == queued["job_id"]
        assert policy.claim("quota-native-blocked") is None

    with policy.connect() as conn:
        assert policy._local_native_active(conn) == 1

    # A memory-only run remains available while the native slot is full.
    extra_memory = _admit(policy, oracle_id, "memory")
    memory_claim = policy.claim("quota-memory")
    assert memory_claim and memory_claim["run_id"] == extra_memory["run_id"]
    assert policy.finish(
        UUID(extra_memory["run_id"]), "quota-memory",
        memory_claim["lease_generation"], witness,
    )

    # Expiring one test-only lease releases admission, not the prior worker's
    # authority to produce a successful receipt.
    with policy.connect() as conn:
        if run_claim:
            conn.execute(
                "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
                "WHERE id = %s", (UUID(run_claim["run_id"]),),
            )
        else:
            conn.execute(
                "UPDATE verification_job SET lease_until = now() - interval '1 second' "
                "WHERE id = %s", (UUID(check_claim["job_id"]),),
            )
    if run_claim:
        replacement = queue.claim("quota-verifier-after-expiry")
        assert replacement and replacement["job_id"] == queued["job_id"]
        assert not policy.finish(
            UUID(run_claim["run_id"]), "quota-native",
            run_claim["lease_generation"], witness,
        )
    else:
        replacement = policy.claim("quota-native-after-expiry")
        assert replacement and replacement["run_id"] == native["run_id"]
        assert not queue.finish(
            UUID(check_claim["job_id"]), "quota-verifier",
            check_claim["lease_generation"], {"status": "REPRODUCED"},
        )
        assert queue.get(UUID(queued["job_id"]))["status"] == "UNKNOWN"
    with policy.connect() as conn:
        assert policy._local_native_active(conn) == 1


def test_project_limit_does_not_block_other_project_native_claims(projects):
    first, first_oracle = projects()
    second, second_oracle = projects()
    a = _admit(first, first_oracle)
    b = _admit(first, first_oracle)
    c = _admit(second, second_oracle)
    claim_a = first.claim("tenant-one-first")
    assert claim_a and claim_a["run_id"] == a["run_id"]
    assert first.claim("tenant-one-second") is None
    claim_c = second.claim("tenant-two-first")
    assert claim_c and claim_c["run_id"] == c["run_id"]
    assert first.claim("tenant-one-third") is None
    assert first.get(UUID(b["run_id"]))["status"] == "PENDING"
    with first.connect() as conn:
        assert first._local_native_active(conn) == 1
    with second.connect() as conn:
        assert second._local_native_active(conn) == 1
