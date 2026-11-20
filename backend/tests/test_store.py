"""Native PostgreSQL integration tests. Set CLASHBOLT_TEST_DATABASE_URL to run."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.worker import process_one


@pytest.fixture
def ledger():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL for native PostgreSQL integration")
    result = Policy(dsn)
    result.migrate()
    # Only this dedicated test database is touched; each test begins isolated.
    with result.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposed = result.propose("fixture-proposer")
    approved = result.approve(UUID(proposed["id"]), ORACLE_HASH, "fixture-reviewer")
    result.approval_id = UUID(approved["id"])
    return result


def admit(ledger, **kwargs):
    return ledger.admit(approved_oracle_id=ledger.approval_id, **kwargs)


def test_admission_is_idempotent_and_rejects_conflicting_payloads(ledger):
    key = str(uuid4())
    first = admit(ledger, client_key=key, variant="racy", schedule="overlap")
    second = admit(ledger, client_key=key, variant="racy", schedule="overlap")
    assert first["run_id"] == second["run_id"]
    assert first["status"] == "PENDING"
    with pytest.raises(ValueError, match="Idempotency"):
        admit(ledger, client_key=key, variant="locked", schedule="overlap")
    assert [e["status"] for e in ledger.transitions(UUID(first["run_id"]))] == ["PENDING"]


def test_worker_publishes_one_result_and_transitions(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    assert asyncio.run(process_one(ledger, "integration-worker"))
    result = ledger.get(UUID(run["run_id"]))
    assert result["status"] == "COMPLETE"
    assert result["verdict"] == "FAIL"
    assert result["consumed_steps"] == 4
    assert result["step_budget"] == 4
    assert [e["status"] for e in ledger.transitions(UUID(run["run_id"]))] == [
        "PENDING", "RUNNING", "COMPLETE"
    ]


def test_expired_generation_is_fenced(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
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
    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="serial")
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"

    active = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
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
        first = admit(ledger, client_key=key, variant="racy", schedule="overlap")
        with pytest.raises(ValueError, match="quota"):
            admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
        ledger.cancel(UUID(first["run_id"]))
    finally:
        with ledger.connect() as conn:
            conn.execute("UPDATE project_quota SET max_open = 20 WHERE project_id = 'local'")


def test_forged_witness_is_never_accepted_as_failure(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
    claim = ledger.claim("checker-test")
    assert claim and claim["run_id"] == run["run_id"]
    forged = asyncio.run(execute("locked", "serial"))
    forged["verdict"] = "FAIL"
    assert ledger.finish(UUID(run["run_id"]), "checker-test", claim["lease_generation"], forged)
    result = ledger.get(UUID(run["run_id"]))
    assert result["status"] == "UNKNOWN"
    assert result["verdict"] == "UNKNOWN"


def test_recent_runs_are_resumable(ledger):
    first = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="serial")
    second = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="overlap")
    listed = ledger.recent(10)
    ids = {item["run_id"] for item in listed}
    assert {first["run_id"], second["run_id"]}.issubset(ids)
    assert all("lease_owner" not in item for item in listed)
    with pytest.raises(ValueError, match="limit"):
        ledger.recent(101)


def test_concurrent_duplicate_admissions_use_one_run(ledger):
    key = str(uuid4())

    def admit_once(_):
        return admit(ledger, client_key=key, variant="racy", schedule="overlap")

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(admit_once, range(8)))
    assert len({item["run_id"] for item in accepted}) == 1
    assert len(ledger.recent()) == 1


def test_concurrent_quota_admission_fails_closed(ledger):
    with ledger.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) VALUES ('local', 1) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = EXCLUDED.max_open"
        )

    def attempt(_):
        try:
            return admit(ledger, 
                client_key=str(uuid4()), variant="locked", schedule="serial"
            )["run_id"]
        except ValueError as error:
            assert "quota" in str(error)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert len([item for item in outcomes if item is not None]) == 1


def test_expired_inflight_cancellation_becomes_unknown(ledger):
    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claimed = ledger.claim("stall-worker")
    assert claimed and claimed["run_id"] == queued["run_id"]
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLING"
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
            "WHERE id = %s", (UUID(queued["run_id"]),),
        )
    assert ledger.reconcile_expired_cancelled() == 1
    current = ledger.get(UUID(queued["run_id"]))
    assert current["status"] == "UNKNOWN"
    assert current["verdict"] == "UNKNOWN"



def test_admission_rejects_unapproved_rule(ledger):
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(client_key=str(uuid4()), variant="racy", schedule="overlap")
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(
            client_key=str(uuid4()), variant="racy", schedule="overlap",
            approved_oracle_id=uuid4(),
        )


def test_oracle_revocation_withdraws_existing_verdict(ledger):
    run = ledger.admit(
        approved_oracle_id=ledger.approval_id,
        client_key=str(uuid4()), variant="racy", schedule="overlap",
    )
    claim = ledger.claim("revocation-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(run["run_id"]), "revocation-worker", claim["lease_generation"], witness
    )
    assert ledger.get(UUID(run["run_id"]))["verdict"] == "FAIL"
    assert ledger.revoke_rule(ledger.approval_id)
    after = ledger.get(UUID(run["run_id"]))
    assert after["verdict"] == "UNKNOWN"
    assert after["status"] == "UNKNOWN"
    assert ledger.transitions(UUID(run["run_id"]))[-1]["note"].startswith(
        "oracle approval revoked"
    )
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(
            approved_oracle_id=ledger.approval_id,
            client_key=str(uuid4()), variant="racy", schedule="overlap",
        )


def test_step_budget_excess_never_certifies_oversized_trace(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("budget-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    witness["observed_steps"].extend(witness["observed_steps"])
    assert ledger.finish(
        UUID(run["run_id"]), "budget-worker", claim["lease_generation"], witness
    )
    saved = ledger.get(UUID(run["run_id"]))
    assert saved["status"] == "UNKNOWN"
    assert saved["verdict"] == "UNKNOWN"
    assert saved["consumed_steps"] == 8


def test_revoked_witness_replay_cannot_regain_certification(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("replay-worker")
    assert claim and claim["run_id"] == queued["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(queued["run_id"]), "replay-worker", claim["lease_generation"], witness
    )
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    with TestClient(create_app()) as client:
        accepted = client.post(f"/api/runs/{queued['run_id']}/replay")
        assert accepted.status_code == 200
        assert accepted.json()["replay_status"] == "REPRODUCED"
        assert ledger.revoke_rule(ledger.approval_id)
        withdrawn = client.post(f"/api/runs/{queued['run_id']}/replay")
        assert withdrawn.status_code == 200
        assert withdrawn.json()["replay_status"] == "UNCONFIRMED"
        assert withdrawn.json()["adjudicated_verdict"] == "UNKNOWN"
