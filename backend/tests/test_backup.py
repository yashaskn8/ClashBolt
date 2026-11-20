import os
import shutil
from pathlib import Path

import pytest
from clashbolt.backup import create_backup, verify_backup


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
