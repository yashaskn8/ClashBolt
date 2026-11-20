"""Restore only a disposable, separate CI database; never modify source."""

import os
import shutil
import subprocess
from uuid import uuid4

import psycopg
import pytest
from clashbolt.backup import backup_control
from clashbolt.policy import Policy
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

    Policy(control).migrate()
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
            assert connection.execute(
                "SELECT count(*) FROM experiment_run"
            ).fetchone() is not None
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
