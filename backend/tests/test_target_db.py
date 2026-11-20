"""Native isolated-cluster tests (opt-in via CLASHBOLT_TEST_TARGET_ADMIN_URL)."""

import os
from uuid import UUID, uuid4

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
    with pytest.raises(ValueError, match="independent"):
        validate_target_cluster(
            "postgresql://user:pwd@198.51.100.1:5433/testing", same
        )


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
    from clashbolt.worker import process_one
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
        replay = client.post(f"/api/runs/{run_id}/replay", headers=headers)
        assert replay.status_code == 200
        assert replay.json()["replay_status"] == "REPRODUCED"
    with psycopg.connect(admin, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s",
            (f"cb_target_{UUID(run_id).hex}",),
        ).fetchone()[0] == 0
