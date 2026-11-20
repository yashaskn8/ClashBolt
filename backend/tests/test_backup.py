import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from clashbolt.backup import _pg_environment, create_backup, verify_backup
from clashbolt.policy import Policy
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def backup_database():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn or not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("Native PostgreSQL backup integration tools not installed")
    return dsn


def test_native_backup_is_integrity_checked_and_non_destructive(
    backup_database, tmp_path: Path,
):
    destination = tmp_path / "control.dump"
    manifest = create_backup(backup_database, destination)
    assert destination.is_file()
    assert manifest["scope"] == "LOCAL_CONTROL_DATABASE"
    assert manifest["includes_secrets"]
    assert len(manifest["sha256"]) == 64
    assert verify_backup(destination) == manifest
    if os.name == "posix":
        assert destination.stat().st_mode & 0o077 == 0
        sidecar = tmp_path / "control.dump.manifest.json"
        assert sidecar.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        create_backup(backup_database, destination)
    with destination.open("ab") as modified:
        modified.write(b"changed")
    with pytest.raises(ValueError, match="digest"):
        verify_backup(destination)


def test_backup_cannot_connect_to_a_remote_database(tmp_path: Path):
    with pytest.raises(ValueError, match="loopback"):
        create_backup(
            "postgresql://tester:secret@198.51.100.24:5432/not-local",
            tmp_path / "remote.dump",
        )


def test_checked_dump_restores_to_disposable_native_cluster(
    backup_database, tmp_path: Path,
):
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not admin:
        pytest.skip("Independent native PostgreSQL cluster required")

    Policy(backup_database).migrate()
    archive = tmp_path / "recovery.dump"
    create_backup(backup_database, archive)
    assert verify_backup(archive)["size_bytes"] > 0

    dbname = f"cb_restore_{uuid4().hex}"
    destination = make_conninfo(admin, dbname=dbname)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname))
        )
    try:
        restored = subprocess.run(
            [
                "pg_restore", "--no-owner", "--no-acl",
                "--dbname", dbname, str(archive),
            ],
            env=_pg_environment(destination),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            text=True,
        )
        assert restored.returncode == 0, restored.stderr[-2000:]
        with psycopg.connect(destination) as connection:
            revisions = dict(connection.execute(
                "SELECT component, version FROM schema_revision"
            ).fetchall())
            assert revisions["control"] >= 4
            assert revisions["policy"] >= 5
            assert connection.execute(
                "SELECT count(*) FROM experiment_run"
            ).fetchone() is not None
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(dbname)
                )
            )
