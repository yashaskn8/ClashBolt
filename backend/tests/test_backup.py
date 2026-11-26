"""Native PostgreSQL backups are generated only from disposable CI databases."""

import json
import os
import shutil

import pytest
from clashbolt.backup import _database_args, backup_control


def test_rejects_remote_or_system_backups():
    with pytest.raises(ValueError, match="loopback"):
        _database_args("postgresql://admin:secret@198.51.100.12:5432/workspace")
    with pytest.raises(ValueError, match="system database"):
        _database_args("postgresql://admin:secret@127.0.0.1:5432/postgres")


def test_custom_archive_roundtrip_read_and_manifest(tmp_path):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn or not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        pytest.skip("Native PostgreSQL clients and CI test database required")
    destination = tmp_path / "control.dump"
    result = backup_control(destination, dsn)
    assert result["archive_bytes"] > 0
    assert len(result["archive_sha256"]) == 64
    assert result["restore_status"] == "NOT_ATTEMPTED"
    assert destination.read_bytes().startswith(b"PGDMP")
    sidecar = json.loads((tmp_path / "control.sha256.json").read_text())
    assert sidecar == result
    assert "secret" not in str(sidecar).lower()
    with pytest.raises(FileExistsError):
        backup_control(destination, dsn)



def test_streaming_disposable_restore_drill_does_not_change_control_database(
    tmp_path, monkeypatch,
):
    import psycopg
    from clashbolt import backup
    from clashbolt.policy import Policy

    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    target = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not dsn or not target or not shutil.which("pg_restore"):
        pytest.skip("Separate native PostgreSQL clusters are required")
    Policy(dsn).migrate()
    archive = tmp_path / "control.dump"
    backup_control(archive, dsn)
    run = backup.subprocess.run

    def check_restore_input(command, **kwargs):
        assert command[0] == "pg_restore"
        assert str(archive) not in command
        stream = kwargs["stdin"]
        assert stream.tell() == 0
        assert stream.read(5) == b"PGDMP"
        stream.seek(0)
        return run(command, **kwargs)

    monkeypatch.setattr(backup.subprocess, "run", check_restore_input)
    result = backup.verify_disposable_restore(archive, dsn, target)
    assert result["status"] == "RESTORE_VERIFIED"
    assert result["restored_schema_revisions"]["control"] >= 1
    with psycopg.connect(target, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (result["scratch_database"],),
        ).fetchone()
        assert exists is None
    archive.write_bytes(b"not the original archive")
    with pytest.raises(ValueError, match="manifest"):
        backup.verify_disposable_restore(archive, dsn, target)
