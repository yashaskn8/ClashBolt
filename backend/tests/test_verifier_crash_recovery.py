"""Crash a real verification worker after provisioning its disposable target.

This exercise uses only a locally configured disposable CI/test database and
the shipped synthetic fixture. It never kills PostgreSQL or probes external apps.
"""

import asyncio
import os
import subprocess
import sys
import time
from contextlib import suppress
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.target_db import drop_abandoned_target
from clashbolt.verification_jobs import VerificationJobs
from clashbolt.worker import reconcile_verification_targets

# This child runs the real verification worker and target provisioning path.
# Pause only after the real ACTIVE record is committed, then the parent
# terminates the entire process before any verification receipt can be saved.
CRASHING_VERIFIER = """
import asyncio
import os
import time
from pathlib import Path

from clashbolt.policy import Policy
from clashbolt.verification_jobs import VerificationJobs
from clashbolt.worker import process_verification_one

original = VerificationJobs.mark_target


def stop_after_active(self, instance_id, status):
    original(self, instance_id, status)
    if status == "ACTIVE":
        Path(os.environ["CLASHBOLT_CRASH_MARKER"]).write_text(str(instance_id))
        while True:
            time.sleep(0.1)


VerificationJobs.mark_target = stop_after_active
asyncio.run(process_verification_one(
    Policy(os.environ["CLASHBOLT_TEST_DATABASE_URL"]), "killed-verifier"
))
"""


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX process termination")
def test_killed_verifier_reclaims_real_target_and_never_accepts_receipt(
    tmp_path, monkeypatch,
):
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not admin or not control:
        pytest.skip("Two disposable native PostgreSQL test clusters are required")

    ledger = Policy(control)
    ledger.migrate()
    with ledger.connect() as conn:
        # No shared or production data: configured test database only.
        assert conn.info.dbname == "clashbolt_test"
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )

    proposal = ledger.propose("verifier-crash-test")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=UUID(approved["id"]),
    )
    claimed = ledger.claim("fixture-worker")
    assert claimed and claimed["run_id"] == run["run_id"]
    assert ledger.finish(
        UUID(run["run_id"]), "fixture-worker", claimed["lease_generation"],
        asyncio.run(execute("locked", "serial")),
    )

    queue = VerificationJobs(control)
    submitted = queue.submit(UUID(run["run_id"]), "REPLAY")
    job_id = UUID(submitted["job_id"])
    marker = tmp_path / "target-ready"
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    child_env = {
        **os.environ,
        "CLASHBOLT_DATABASE_URL": control,
        "CLASHBOLT_CRASH_MARKER": str(marker),
    }
    child = subprocess.Popen(
        [sys.executable, "-c", CRASHING_VERIFIER],
        env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    registered = []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker.is_file():
            if child.poll() is not None:
                pytest.fail("Verifier exited before the ACTIVE crash point")
            time.sleep(0.05)
        assert marker.is_file(), "Verifier did not reach ACTIVE crash point"
        instance_id = UUID(marker.read_text())
        with ledger.connect() as conn:
            target = conn.execute(
                "SELECT status, lease_generation FROM verification_target_instance "
                "WHERE id = %s AND job_id = %s",
                (instance_id, job_id),
            ).fetchone()
            job = conn.execute(
                "SELECT status, lease_owner, lease_generation "
                "FROM verification_job WHERE id = %s", (job_id,),
            ).fetchone()
        assert target and target["status"] == "ACTIVE"
        assert job and job["status"] == "RUNNING"
        assert job["lease_owner"] == "killed-verifier"
        assert target["lease_generation"] == job["lease_generation"]
        with psycopg.connect(admin, autocommit=True) as db:
            assert db.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{instance_id.hex}",),
            ).fetchone()[0] == 1

        # Abrupt process loss bypasses Python contextmanager cleanup.
        child.kill()
        assert child.wait(timeout=5) != 0
        with psycopg.connect(admin, autocommit=True) as db:
            assert db.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{instance_id.hex}",),
            ).fetchone()[0] == 1

        # Advance only the CI test ledger's clock fields; no long sleeps.
        with ledger.connect() as conn:
            conn.execute(
                "UPDATE verification_job "
                "SET lease_until = now() - interval '1 second' WHERE id = %s",
                (job_id,),
            )
            conn.execute(
                "UPDATE verification_target_instance "
                "SET updated_at = now() - interval '10 seconds' WHERE id = %s",
                (instance_id,),
            )
        assert reconcile_verification_targets(queue, ledger) == 1
        with psycopg.connect(admin, autocommit=True) as db:
            assert db.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{instance_id.hex}",),
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = %s",
                (f"cb_runner_{instance_id.hex}",),
            ).fetchone()[0] == 0
        with ledger.connect() as conn:
            assert conn.execute(
                "SELECT status FROM verification_target_instance WHERE id = %s",
                (instance_id,),
            ).fetchone()["status"] == "CLEANED"
        # The lost verifier has no authority to certify a replay.
        assert not queue.finish(
            job_id, "killed-verifier", job["lease_generation"],
            {"status": "REPRODUCED"},
        )
        assert queue.claim("replacement-worker") is None
        assert queue.get(job_id)["status"] == "UNKNOWN"
        assert queue.get(job_id)["result"]["status"] == "UNCONFIRMED"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        with ledger.connect() as conn:
            registered = [
                row["id"] for row in conn.execute(
                    "SELECT id FROM verification_target_instance "
                    "WHERE job_id = %s", (job_id,),
                ).fetchall()
            ]
        # Even an interrupted test must not leak a target database or role.
        for instance_id in registered:
            with suppress(psycopg.Error):
                drop_abandoned_target(admin, control, instance_id)
