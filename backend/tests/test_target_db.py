"""Native isolated-cluster tests (opt-in via CLASHBOLT_TEST_TARGET_ADMIN_URL)."""

import os
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
import pytest
from clashbolt.target_db import disposable_target, validate_target_cluster


@pytest.fixture
def cluster():
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not admin or not control:
        pytest.skip("Separate native PostgreSQL cluster is required")
    return admin, control


def test_same_cluster_and_remote_are_rejected():
    same = "postgresql://user:pwd@127.0.0.1:5432/testing"
    with pytest.raises(ValueError, match="independent"):
        validate_target_cluster(same, same)
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(
            "postgresql://user:pwd@198.51.100.1:5433/testing", same
        )


def test_loopback_alias_cannot_reuse_control_database_port():
    control = "postgresql://user:pwd@127.0.0.1:5432/control"
    for alias in ("127.0.0.2", "::1"):
        host = f"[{alias}]" if ":" in alias else alias
        with pytest.raises(ValueError, match="independent"):
            validate_target_cluster(
                f"postgresql://user:pwd@{host}:5432/postgres", control
            )
    # libpq may accept this abbreviated address, but our validator rejects
    # noncanonical spelling before attempting a connection.
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(
            "postgresql://user:pwd@127.1:5432/postgres", control
        )


def test_libpq_hostaddr_and_service_cannot_redirect_target(monkeypatch):
    control = "postgresql://user:pwd@127.0.0.1:5432/control"
    target = "postgresql://admin:pwd@localhost:5433/postgres"
    validate_target_cluster(target, control)

    remote_hostaddr = target + "?hostaddr=198.51.100.1"
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(remote_hostaddr, control)
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(
            target,
            control + "?hostaddr=198.51.100.1",
        )
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(target + "?service=remote-target", control)

    monkeypatch.setenv("PGHOSTADDR", "198.51.100.1")
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(target, control)
    monkeypatch.delenv("PGHOSTADDR")
    monkeypatch.setenv("PGSERVICE", "remote-target")
    with pytest.raises(ValueError, match="loopback"):
        validate_target_cluster(target, control)


def test_inherited_postgres_port_is_compared(monkeypatch):
    control = "postgresql://user:pwd@127.0.0.1:5432/control"
    target = "postgresql://admin:pwd@localhost/postgres"
    monkeypatch.setenv("PGPORT", "5432")
    with pytest.raises(ValueError, match="independent"):
        validate_target_cluster(target, control)
    monkeypatch.setenv("PGPORT", "5433")
    validate_target_cluster(target, control)


def test_target_database_is_per_run_and_is_dropped(cluster):
    admin, control = cluster
    first, second = uuid4(), uuid4()
    with disposable_target(admin, control, first) as a:
        with disposable_target(admin, control, second) as b:
            assert a.database != b.database
            assert a.role != b.role
            with psycopg.connect(a.url) as first_db:
                first_db.execute(
                    "UPDATE inventory SET confirmed_count = 1 WHERE id = 1"
                )
            with psycopg.connect(b.url) as second_db:
                row = second_db.execute(
                    "SELECT remaining, confirmed_count FROM inventory"
                ).fetchone()
                assert row == (1, 0)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    second_db.execute("DROP TABLE inventory")
        with psycopg.connect(a.url) as still_active:
            assert still_active.execute(
                "SELECT confirmed_count FROM inventory"
            ).fetchone()[0] == 1
    # The target database must no longer exist after the context exits.
    with psycopg.connect(admin, autocommit=True) as connection:
        names = connection.execute(
            "SELECT datname FROM pg_database WHERE datname IN (%s, %s)",
            (f"cb_target_{first.hex}", f"cb_target_{second.hex}"),
        ).fetchall()
        assert names == []


def test_exception_still_drops_target(cluster):
    admin, control = cluster
    run_id = uuid4()
    with pytest.raises(RuntimeError, match="synthetic fault"):
        with disposable_target(admin, control, run_id):
            raise RuntimeError("synthetic fault")
    with psycopg.connect(admin, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s",
            (f"cb_target_{run_id.hex}",),
        ).fetchone()[0] == 0



def test_disposable_sql_fixture_matches_synthetic_contract(cluster):
    import asyncio

    from clashbolt.experiment import execute_postgres
    from clashbolt.witness import validate_contract

    admin, control = cluster
    with disposable_target(admin, control, uuid4()) as target:
        broken = asyncio.run(execute_postgres("racy", "overlap", target.url))
        assert broken["execution_backend"] == "native-postgresql"
        assert broken["verdict"] == "FAIL"
        assert validate_contract(broken) is None
    with disposable_target(admin, control, uuid4()) as target:
        corrected = asyncio.run(execute_postgres("locked", "overlap", target.url))
        assert corrected["verdict"] == "PASS"
        assert validate_contract(corrected) is None


def test_native_target_worker_and_replay_end_to_end(cluster, monkeypatch):
    import asyncio

    from clashbolt.api import create_app
    from clashbolt.experiment import ORACLE_HASH
    from clashbolt.policy import Policy
    from clashbolt.worker import process_one, process_verification_one
    from fastapi.testclient import TestClient

    admin, control = cluster
    monkeypatch.setenv("CLASHBOLT_TEST_TARGET_ADMIN_URL", admin)
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", control)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "target-ci-token")
    ledger = Policy(control)
    ledger.migrate()
    with ledger.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposed = ledger.propose("target-test")
    approved = ledger.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    payload = {
        "client_key": str(uuid4()), "variant": "racy", "schedule": "overlap",
        "execution_backend": "native-postgresql",
        "approved_oracle_id": approved["id"],
    }
    with TestClient(create_app()) as client:
        catalog = client.get("/api/catalog").json()
        assert "native-postgresql" in catalog["execution_backends"]
        headers = {"X-Operator-Token": "target-ci-token"}
        response = client.post("/api/runs", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        assert asyncio.run(process_one(ledger, "native-test-worker"))
        completed = client.get(f"/api/runs/{run_id}").json()
        assert completed["status"] == "COMPLETE"
        assert completed["verdict"] == "FAIL"
        assert completed["execution_backend"] == "native-postgresql"
        assert completed["witness"]["execution_backend"] == "native-postgresql"
        targets = ledger.target_instances(UUID(run_id))
        assert len(targets) == 1
        assert targets[0]["status"] == "CLEANED"
        # Native verification cannot run directly inside the API process.
        replay = client.post(f"/api/runs/{run_id}/replay", headers=headers)
        assert replay.status_code == 409
        reduced = client.post(f"/api/runs/{run_id}/reduce", headers=headers)
        assert reduced.status_code == 409

        # API has only a safe cluster locator; admin secret stays with worker.
        monkeypatch.setenv(
            "CLASHBOLT_TARGET_CLUSTER_URL",
            "postgresql://127.0.0.1:5433/postgres",
        )
        monkeypatch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)
        replay_job = client.post(
            f"/api/runs/{run_id}/verification-jobs",
            json={"kind": "REPLAY"}, headers=headers,
        )
        assert replay_job.status_code == 202, replay_job.text
        reduction_job = client.post(
            f"/api/runs/{run_id}/verification-jobs",
            json={"kind": "REDUCE"}, headers=headers,
        )
        assert reduction_job.status_code == 202, reduction_job.text

        # Simulate the separately configured native worker environment.
        monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
        assert asyncio.run(process_verification_one(ledger, "verifier-worker"))
        assert asyncio.run(process_verification_one(ledger, "verifier-worker"))
        monkeypatch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)

        verified = client.get(
            f"/api/verification-jobs/{replay_job.json()['job_id']}"
        ).json()
        assert verified["status"] == "COMPLETE", verified
        assert verified["result"]["status"] == "REPRODUCED"
        minimized = client.get(
            f"/api/verification-jobs/{reduction_job.json()['job_id']}"
        ).json()
        assert minimized["status"] == "COMPLETE", minimized
        assert minimized["result"]["status"] == "MINIMAL"
        assert minimized["result"]["minimal_gate_count"] == 4

    # Every native replay/reduction scratch target is registered before
    # provisioning and must be marked cleaned by the independent worker.
    with ledger.connect() as conn:
        verification_targets = conn.execute(
            """
            SELECT id, status FROM verification_target_instance
            WHERE job_id IN (%s, %s)
            """,
            (UUID(replay_job.json()["job_id"]), UUID(reduction_job.json()["job_id"])),
        ).fetchall()
    assert len(verification_targets) >= 2
    assert all(row["status"] == "CLEANED" for row in verification_targets)
    with psycopg.connect(admin, autocommit=True) as connection:
        for row in verification_targets:
            assert connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{row['id'].hex}",),
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s",
            (
                f"cb_target_{uuid5(NAMESPACE_URL, f'clashbolt:{run_id}:1').hex}",
            ),
        ).fetchone()[0] == 0


def test_expired_native_target_is_reclaimed(cluster, monkeypatch):
    from clashbolt.experiment import ORACLE_HASH
    from clashbolt.policy import Policy
    from clashbolt.worker import reconcile_targets

    admin, control = cluster
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    ledger = Policy(control)
    ledger.migrate()
    with ledger.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposal = ledger.propose("orphan-proposer")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", control)
    run = ledger.admit(
        client_key=str(uuid4()), variant="racy", schedule="overlap",
        approved_oracle_id=UUID(approved["id"]),
        execution_backend="native-postgresql",
    )
    claimed = ledger.claim("crashed-worker")
    assert claimed
    run_id = UUID(run["run_id"])
    instance = uuid5(
        NAMESPACE_URL, f"clashbolt:{run_id}:{claimed['lease_generation']}"
    )
    assert ledger.track_target(
        run_id, claimed["lease_generation"], "crashed-worker", instance
    )
    with disposable_target(admin, control, instance) as abandoned:
        ledger.mark_target(instance, "ACTIVE")
        with ledger.connect() as conn:
            conn.execute(
                "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
                "WHERE id = %s", (run_id,),
            )
            conn.execute(
                "UPDATE target_instance SET updated_at = now() - interval '10 seconds' "
                "WHERE id = %s", (instance,),
            )
        assert reconcile_targets(ledger) == 1
        assert ledger.target_instances(run_id)[0]["status"] == "CLEANED"
        with psycopg.connect(admin, autocommit=True) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (abandoned.database,),
            ).fetchone()[0] == 0


def test_native_admission_uses_safe_endpoint_without_admin_secret(
    cluster, monkeypatch,
):
    from clashbolt.experiment import ORACLE_HASH
    from clashbolt.policy import Policy
    from psycopg.conninfo import conninfo_to_dict

    admin, control = cluster
    location = conninfo_to_dict(admin)
    safe_endpoint = (
        f"postgresql://127.0.0.1:{location.get('port', 5432)}/postgres"
    )
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", safe_endpoint)
    monkeypatch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)
    ledger = Policy(control)
    ledger.migrate()
    proposal = ledger.propose("separate-worker-secret")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=UUID(approved["id"]),
        execution_backend="native-postgresql",
    )
    assert run["execution_backend"] == "native-postgresql"
    assert "password" not in safe_endpoint


def test_runner_credential_and_database_privileges(cluster):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin, control = cluster
    private_admin = make_conninfo(
        admin, sslpassword="admin-client-key-secret", application_name="provisioner",
        options="-c statement_timeout=10000",
    )
    with disposable_target(private_admin, control, uuid4()) as first:
        with disposable_target(admin, control, uuid4()) as other:
            settings = conninfo_to_dict(first.url)
            assert settings["user"] == first.role
            assert not {"sslpassword", "application_name", "options"} & settings.keys()
            assert settings["password"] not in repr(first)
            with pytest.raises(psycopg.OperationalError, match="permission denied"):
                psycopg.connect(make_conninfo(first.url, dbname=other.database))
            with psycopg.connect(first.url, autocommit=True) as runner:
                for statement in (
                    "CREATE TEMP TABLE scratch(id int)",
                    "CREATE TABLE public.scratch(id int)",
                    "UPDATE inventory SET id = 2 WHERE id = 1",
                    "DELETE FROM inventory",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        runner.execute(statement)
                runner.execute("UPDATE inventory SET remaining = 0 WHERE id = 1")
                with psycopg.connect(first.url):
                    with pytest.raises(psycopg.OperationalError, match="too many connections"):
                        psycopg.connect(first.url)


def test_expired_verification_target_is_removed_from_real_cluster(cluster, monkeypatch):
    import asyncio

    from clashbolt.experiment import ORACLE_HASH, execute
    from clashbolt.policy import Policy
    from clashbolt.verification_jobs import VerificationJobs
    from clashbolt.worker import reconcile_verification_targets

    admin, control = cluster
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    ledger = Policy(control)
    ledger.migrate()
    with ledger.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposal = ledger.propose("abandoned-verification")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()), variant="racy", schedule="overlap",
        approved_oracle_id=UUID(approved["id"]),
    )
    claimed = ledger.claim("fixture-worker")
    assert claimed
    assert ledger.finish(
        UUID(run["run_id"]), "fixture-worker", claimed["lease_generation"],
        asyncio.run(execute("racy", "overlap")),
    )
    queue = VerificationJobs(control)
    queue.submit(UUID(run["run_id"]), "REPLAY")
    job = queue.claim("crashed-verifier")
    assert job
    instance = uuid5(
        NAMESPACE_URL,
        f"clashbolt:verification:{job['job_id']}:{job['lease_generation']}:1",
    )
    assert queue.track_target(
        UUID(job["job_id"]), "crashed-verifier", job["lease_generation"], instance
    )
    with disposable_target(admin, control, instance) as target:
        queue.mark_target(instance, "ACTIVE")
        with ledger.connect() as conn:
            conn.execute(
                "UPDATE verification_job "
                "SET lease_until = now() - interval '1 second' WHERE id = %s",
                (UUID(job["job_id"]),),
            )
            conn.execute(
                "UPDATE verification_target_instance "
                "SET updated_at = now() - interval '10 seconds' WHERE id = %s",
                (instance,),
            )
        assert reconcile_verification_targets(queue, ledger) == 1
        with psycopg.connect(admin, autocommit=True) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (target.database,),
            ).fetchone()[0] == 0
    with ledger.connect() as conn:
        assert conn.execute(
            "SELECT status FROM verification_target_instance WHERE id = %s",
            (instance,),
        ).fetchone()["status"] == "CLEANED"
