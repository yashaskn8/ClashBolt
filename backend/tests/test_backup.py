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
