import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.verification_jobs import VerificationJobs
from clashbolt.worker import process_verification_one
from fastapi.testclient import TestClient


@pytest.fixture
def completed_run():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL test database required")
    ledger = Policy(dsn)
    ledger.migrate()
    with ledger.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposal = ledger.propose("verification-workshop")
    approval = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()), variant="racy", schedule="overlap",
        approved_oracle_id=UUID(approval["id"]),
    )
    claimed = ledger.claim("fixture-worker")
    assert claimed
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(run["run_id"]), "fixture-worker", claimed["lease_generation"], witness
    )
    return ledger, UUID(run["run_id"]), UUID(approval["id"])


def test_verification_jobs_claim_once_and_invalidate_after_revocation(completed_run):
    ledger, run_id, approval_id = completed_run
    queue = VerificationJobs(ledger.dsn)
    a = queue.submit(run_id, "REPLAY")
    assert a == queue.submit(run_id, "REPLAY")
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: queue.claim("verifier"), range(6)))
    accepted = [claim for claim in claims if claim]
    assert len(accepted) == 1
    job = accepted[0]
    witness = ledger.get(run_id)["witness"]
    result = {
        "status": "REPRODUCED",
        "scope": "SYNTHETIC_ONLY",
        "original_sha256": witness["evidence_sha256"],
        "replay_sha256": witness["evidence_sha256"],
        "verdict": "FAIL",
    }
    assert queue.finish(
        UUID(job["job_id"]), "verifier", job["lease_generation"], result,
    )
    assert not queue.finish(
        UUID(job["job_id"]), "verifier", job["lease_generation"], result,
    )
    assert queue.get(UUID(job["job_id"]))["status"] == "COMPLETE"
    assert ledger.revoke_rule(approval_id)
    stale = queue.get(UUID(job["job_id"]))
    assert stale["status"] == "UNKNOWN"
    assert stale["result"]["status"] == "UNCONFIRMED"


def test_revoked_run_cannot_complete_reduction_job(completed_run):
    ledger, run_id, approval_id = completed_run
    queue = VerificationJobs(ledger.dsn)
    claimed = queue.claim("verifier")
    assert claimed is None
    scheduled = queue.submit(run_id, "REDUCE")
    job = queue.claim("verifier")
    assert job and scheduled["job_id"] == job["job_id"]
    assert ledger.revoke_rule(approval_id)
    assert queue.finish(
        UUID(job["job_id"]), "verifier", job["lease_generation"],
        {
            "status": "MINIMAL",
            "original_evidence_sha256": "invalid-revoked-evidence",
        },
    )
    assert queue.get(UUID(job["job_id"]))["status"] == "UNKNOWN"
    with pytest.raises(ValueError, match="certified"):
        queue.submit(run_id, "REPLAY")


def test_verification_route_and_separate_worker(completed_run, monkeypatch):
    ledger, run_id, _ = completed_run
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "verify-token")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    with TestClient(create_app()) as client:
        path = f"/api/runs/{run_id}/verification-jobs"
        assert client.post(path, json={"kind": "REPLAY"}).status_code == 403
        response = client.post(
            path, json={"kind": "REPLAY"},
            headers={"X-Operator-Token": "verify-token"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert client.get(f"/api/verification-jobs/{job_id}").json()["status"] == "PENDING"
        assert asyncio.run(process_verification_one(ledger, "independent-worker"))
        completed = client.get(f"/api/verification-jobs/{job_id}").json()
        assert completed["status"] == "COMPLETE"
        assert completed["result"]["status"] == "REPRODUCED"
        assert client.post(
            path, json={"kind": "UNSAFE"}, headers={"X-Operator-Token": "verify-token"}
        ).status_code == 422


def test_forged_verification_status_without_evidence_does_not_certify(completed_run):
    ledger, run_id, _ = completed_run
    queue = VerificationJobs(ledger.dsn)
    queue.submit(run_id, "REPLAY")
    claim = queue.claim("forged-worker")
    assert claim is not None
    assert queue.finish(
        UUID(claim["job_id"]), "forged-worker", claim["lease_generation"],
        {"status": "REPRODUCED"},
    )
    current = queue.get(UUID(claim["job_id"]))
    assert current["status"] == "UNKNOWN"
    assert current["result"]["status"] == "UNCONFIRMED"
    assert "evidence" in current["result"]["reason"]


def test_verification_targets_are_generation_bound_and_reclaimed(completed_run):
    ledger, run_id, _ = completed_run
    queue = VerificationJobs(ledger.dsn)
    queue.submit(run_id, "REPLAY")
    claim = queue.claim("verification-owner")
    assert claim is not None
    job_id = UUID(claim["job_id"])
    generation = claim["lease_generation"]
    instance = uuid4()

    assert queue.track_target(job_id, "verification-owner", generation, instance)
    queue.mark_target(instance, "ACTIVE")
    # Age the row but keep its worker lease live: active work cannot be stolen.
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE verification_target_instance "
            "SET updated_at = now() - interval '10 seconds' WHERE id = %s",
            (instance,),
        )
    assert queue.claim_abandoned_targets() == []

    # A killed worker's lease expires, making its previously tracked target
    # independently recoverable without knowing the old worker's credentials.
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE verification_job SET lease_until = now() - interval "
            "'1 second' WHERE id = %s",
            (job_id,),
        )
    assert not queue.track_target(job_id, "verification-owner", generation, uuid4())
    assert queue.claim_abandoned_targets() == [instance]
    with ledger.connect() as conn:
        status = conn.execute(
            "SELECT status FROM verification_target_instance WHERE id = %s",
            (instance,),
        ).fetchone()["status"]
    assert status == "CLEANING"
    queue.mark_target(instance, "CLEANED")
    assert queue.claim_abandoned_targets() == []
    assert queue.claim("new-verifier") is None
    assert queue.get(job_id)["status"] == "UNKNOWN"


def test_verification_target_registration_rejects_stale_owners(completed_run):
    ledger, run_id, _ = completed_run
    queue = VerificationJobs(ledger.dsn)
    queue.submit(run_id, "REDUCE")
    claim = queue.claim("first-owner")
    assert claim
    job_id = UUID(claim["job_id"])
    assert not queue.track_target(
        job_id, "other-owner", claim["lease_generation"], uuid4()
    )
    assert not queue.track_target(
        job_id, "first-owner", claim["lease_generation"] + 1, uuid4()
    )
    with pytest.raises(ValueError, match="limit"):
        queue.claim_abandoned_targets(0)
    with pytest.raises(ValueError, match="limit"):
        queue.claim_abandoned_targets(51)
    with pytest.raises(ValueError, match="state"):
        queue.mark_target(uuid4(), "PENDING")

def test_expired_verification_read_is_unconfirmed_before_worker_reconciliation(
    completed_run,
):
    ledger, run_id, _ = completed_run
    queue = VerificationJobs(ledger.dsn)
    submitted = queue.submit(run_id, "REPLAY")
    claim = queue.claim("lost-verifier")
    assert claim and claim["job_id"] == submitted["job_id"]
    job_id = UUID(claim["job_id"])
    assert queue.get(job_id)["status"] == "RUNNING"

    with ledger.connect() as conn:
        conn.execute(
            "UPDATE verification_job SET lease_until = now() - interval '1 second' "
            "WHERE id = %s", (job_id,),
        )
    observed = queue.get(job_id)
    assert observed["status"] == "UNKNOWN"
    assert observed["result"]["status"] == "UNCONFIRMED"
    assert "lease expiry" in observed["result"]["reason"]
    # Reading does not mutate the audit; worker reconciliation persists it.
    with ledger.connect() as conn:
        assert conn.execute(
            "SELECT status FROM verification_job WHERE id = %s", (job_id,),
        ).fetchone()["status"] == "RUNNING"
    assert not queue.finish(
        job_id, "lost-verifier", claim["lease_generation"],
        {"status": "REPRODUCED"},
    )
    assert queue.claim("reconcile-after-expiry") is None
    assert queue.get(job_id)["status"] == "UNKNOWN"
    with ledger.connect() as conn:
        assert conn.execute(
            "SELECT status FROM verification_job WHERE id = %s", (job_id,),
        ).fetchone()["status"] == "UNKNOWN"



def test_verification_rejects_receipts_expired_after_transaction_started(
    completed_run, monkeypatch,
):
    ledger, run_id, _ = completed_run
    queue = VerificationJobs(ledger)
    queue.submit(run_id, "REPLAY")
    job = queue.claim("deadline-verifier")
    job_id = UUID(job["job_id"])
    evidence = ledger.get(run_id)["witness"]["evidence_sha256"]
    report = {
        "status": "REPRODUCED", "scope": "SYNTHETIC_ONLY", "verdict": "FAIL",
        "original_sha256": evidence, "replay_sha256": evidence,
    }
    original = ledger.connect

    def transaction_before_expiry():
        transaction = original()
        now = transaction.execute("SELECT now() AS started").fetchone()["started"]
        with original() as conn:
            conn.execute(
                """
                UPDATE verification_job
                SET lease_until = %s::timestamptz + interval '1 microsecond'
                WHERE id = %s
                """,
                (now, job_id),
            )
        return transaction

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "connect", transaction_before_expiry)
        assert not queue.finish(job_id, "deadline-verifier", job["lease_generation"], report)
        assert not queue.track_target(
            job_id, "deadline-verifier", job["lease_generation"], uuid4(),
        )
    assert queue.get(job_id)["status"] == "UNKNOWN"
