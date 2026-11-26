"""Restore only a disposable, separate CI database; never modify source."""

import asyncio
import os
import shutil
import subprocess
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.accounts import AccountStore
from clashbolt.backup import backup_control
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.worker import process_one
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def test_control_backup_cold_restores_to_separate_native_cluster(tmp_path):
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if (
        not control or not admin
        or not shutil.which("pg_dump")
        or not shutil.which("pg_restore")
    ):
        pytest.skip("Two disposable native PostgreSQL clusters are required")

    accounts = AccountStore(control)
    accounts.migrate()
    project_id = f"cold-restore-{uuid4().hex[:12]}"
    schema = accounts.create_project(project_id)
    ledger = Policy(control, schema=schema, project_id=project_id)
    proposal = ledger.propose("cold-restore-operator")
    approval = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "cold-restore-reviewer")
    approved_id = UUID(approval["id"])
    admitted = [
        ledger.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=approved_id,
        )
        for _ in range(2)
    ]
    cancelled_id, pending_id = (UUID(run["run_id"]) for run in admitted)
    assert ledger.cancel(cancelled_id, ledger.get(cancelled_id)['version'])["status"] == "CANCELLED"
    before = {
        run_id: (ledger.get(run_id), ledger.transitions(run_id))
        for run_id in (cancelled_id, pending_id)
    }
    archive = tmp_path / "recover.dump"
    receipt = backup_control(archive, control)
    assert receipt["archive_bytes"] > 0

    name = f"cb_restore_{uuid4().hex}"
    settings = conninfo_to_dict(admin)
    destination = make_conninfo(admin, dbname=name)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        )
    try:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("PG")
        }
        env["PGPASSWORD"] = settings["password"]
        restored = subprocess.run(
            [
                "pg_restore", "--no-owner", "--no-acl",
                "--host", settings["host"],
                "--port", str(settings.get("port", "5432")),
                "--username", settings["user"],
                "--dbname", name, str(archive),
            ],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, check=False, timeout=120,
        )
        assert restored.returncode == 0, restored.stderr[-2000:]
        with psycopg.connect(destination) as connection:
            versions = dict(connection.execute(
                "SELECT component, version FROM schema_revision"
            ).fetchall())
            assert versions["control"] >= 4
            assert versions["policy"] >= 5
        assert AccountStore(destination).project_schema(project_id) == schema
        recovered = Policy(destination, schema=schema, project_id=project_id)
        for run_id, expected in before.items():
            assert (recovered.get(run_id), recovered.transitions(run_id)) == expected
        assert asyncio.run(process_one(recovered, "cold-restore-worker"))
        result = recovered.get(pending_id)
        assert result["status"] == "COMPLETE"
        assert result["verdict"] == "PASS"
        assert [event["status"] for event in recovered.transitions(pending_id)] == [
            "PENDING", "RUNNING", "COMPLETE",
        ]
        for run_id, expected in before.items():
            assert (ledger.get(run_id), ledger.transitions(run_id)) == expected
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
