"""Native PostgreSQL integration tests. Set CLASHBOLT_TEST_DATABASE_URL to run."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import execute
from clashbolt.store import Ledger
from clashbolt.worker import process_one


@pytest.fixture
def ledger():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL for native PostgreSQL integration")
    result = Ledger(dsn)
    result.migrate()
    # Only this dedicated test database is touched; each test begins isolated.
    with result.connect() as conn:
        conn.execute("TRUNCATE run_transition, experiment_run, project_quota CASCADE")
    return result


def test_admission_is_idempotent_and_rejects_conflicting_payloads(ledger):
    key = str(uuid4())
    first = ledger.admit(client_key=key, variant="racy", schedule="overlap")
    second = ledger.admit(client_key=key, variant="racy", schedule="overlap")
    assert first["run_id"] == second["run_id"]
    assert first["status"] == "PENDING"
    with pytest.raises(ValueError, match="Idempotency"):
        ledger.admit(client_key=key, variant="locked", schedule="overlap")
    assert [e["status"] for e in ledger.transitions(UUID(first["run_id"]))] == ["PENDING"]


def test_worker_publishes_one_result_and_transitions(ledger):
    run = ledger.admit(client_key=str(uuid4()), variant="racy", schedule="overlap")
    assert asyncio.run(process_one(ledger, "integration-worker"))
    result = ledger.get(UUID(run["run_id"]))
    assert result["status"] == "COMPLETE"
    assert result["verdict"] == "FAIL"
    assert [e["status"] for e in ledger.transitions(UUID(run["run_id"]))] == [
        "PENDING", "RUNNING", "COMPLETE"
    ]


def test_expired_generation_is_fenced(ledger):
    run = ledger.admit(client_key=str(uuid4()), variant="locked", schedule="serial")
    first = ledger.claim("worker-one")
    assert first is not None
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' WHERE id = %s",
            (UUID(first["run_id"]),),
        )
    second = ledger.claim("worker-two")
    assert second is not None
    assert second["run_id"] == first["run_id"]
    witness = asyncio.run(execute("locked", "serial"))
    assert not ledger.finish(UUID(run["run_id"]), "worker-one", first["lease_generation"], witness)
    assert ledger.finish(UUID(run["run_id"]), "worker-two", second["lease_generation"], witness)
    assert ledger.get(UUID(run["run_id"]))["verdict"] == "PASS"


def test_cancel_pending_and_inflight_unknown(ledger):
    queued = ledger.admit(client_key=str(uuid4()), variant="racy", schedule="serial")
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"

    active = ledger.admit(client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("cancel-worker")
    assert claim and claim["run_id"] == active["run_id"]
    assert ledger.cancel(UUID(active["run_id"]))["status"] == "CANCELLING"
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(active["run_id"]), "cancel-worker", claim["lease_generation"], witness
    )
    result = ledger.get(UUID(active["run_id"]))
    assert result["status"] == "UNKNOWN"
    assert result["verdict"] == "UNKNOWN"


def test_quota_uses_single_serialized_row(ledger):
    key = str(uuid4())
    with ledger.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) VALUES ('local', 1) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = EXCLUDED.max_open"
        )
    try:
        # Other tests may leave active runs: use a unique local workspace test DB per job.
        with ledger.connect() as conn:
            conn.execute("UPDATE experiment_run SET status = 'CANCELLED' WHERE status = 'PENDING'")
        first = ledger.admit(client_key=key, variant="racy", schedule="overlap")
        with pytest.raises(ValueError, match="quota"):
            ledger.admit(client_key=str(uuid4()), variant="racy", schedule="overlap")
        ledger.cancel(UUID(first["run_id"]))
    finally:
        with ledger.connect() as conn:
            conn.execute("UPDATE project_quota SET max_open = 20 WHERE project_id = 'local'")
