"""Backup/restore failure paths must preserve sensitive data and not leave files.

The first tests use a simulated native dump utility. Restore failure cleanup
is exercised against only the two disposable PostgreSQL CI clusters.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest
from clashbolt import backup
from clashbolt.policy import Policy


@pytest.mark.parametrize("dsn,reason", [
    ("postgresql://u:p@198.51.100.10:5432/control", "loopback"),
    ("postgresql://u:p@127.0.0.1:5432/control?hostaddr=198.51.100.10", "loopback"),
    ("postgresql://u@127.0.0.1:5432/control", "password-protected"),
    ("postgresql://u:p@127.0.0.1:5432/postgres", "system database"),
    ("postgresql://u:p@127.0.0.1:5432/template0", "system database"),
    ("postgresql://u:p@127.0.0.1:5432/template1", "system database"),
])
def test_backup_endpoint_rejects_remote_incomplete_and_system_dsns(dsn, reason):
    with pytest.raises(ValueError, match=reason):
        backup._database_args(dsn)


def test_backup_refuses_bad_paths_before_creating_archive(tmp_path):
    dsn = "postgresql://u:p@127.0.0.1:5432/control"
    with pytest.raises(ValueError, match="extension"):
        backup.backup_control(tmp_path / "control.tar", dsn)
    with pytest.raises(ValueError, match="directory"):
        backup.backup_control(tmp_path / "absent" / "control.dump", dsn)
    dest = tmp_path / "control.dump"
    dest.write_text("old contents")
    with pytest.raises(FileExistsError, match="already exists"):
        backup.backup_control(dest, dsn)
    assert dest.read_text() == "old contents"
    dest.unlink()
    (tmp_path / "control.sha256.json").write_text("old manifest")
    with pytest.raises(FileExistsError, match="already exists"):
        backup.backup_control(dest, dsn)


def _mock_native_dump(monkeypatch, *, error=False, invalid=False):
    commands = []
    environments = []

    def simulate(command, **kwargs):
        commands.append(command)
        environments.append(kwargs.get("env"))
        if error:
            raise subprocess.CalledProcessError(1, command)
        if command[0] == "pg_dump":
            path = next(value.split("=", 1)[1] for value in command
                        if value.startswith("--file="))
            Path(path).write_bytes(b"invalid-dump" if invalid else b"PGDMPfixed")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(backup.subprocess, "run", simulate)
    return commands, environments


@pytest.mark.parametrize("error,invalid", [(True, False), (False, True)])
def test_failed_or_invalid_native_dump_leaves_no_archive(
    tmp_path, monkeypatch, error, invalid,
):
    commands, environments = _mock_native_dump(
        monkeypatch, error=error, invalid=invalid,
    )
    monkeypatch.setenv("PGHOST", "untrusted-host")
    monkeypatch.setenv("PGPASSWORD", "should-not-be-used")
    dsn = "postgresql://worker:local-secret@127.0.0.1:5432/control"
    destination = tmp_path / "new.dump"
    with pytest.raises(
        RuntimeError if error else ValueError,
        match="integrity check failed" if error else "custom archive",
    ):
        backup.backup_control(destination, dsn)
    assert not destination.exists()
    assert not (tmp_path / "new.sha256.json").exists()
    assert list(tmp_path.glob("*.partial")) == []
    assert list(tmp_path.glob(".*.partial")) == []
    assert commands and all("local-secret" not in str(command) for command in commands)
    # Only pg_dump needs database credentials. pg_restore's local archive
    # inspection deliberately receives no database environment at all.
    assert len(commands) == len(environments)
    for command, env in zip(commands, environments):
        if command[0] == "pg_dump":
            assert env is not None
            assert env["PGPASSWORD"] == "local-secret"
            assert "PGHOST" not in env
        else:
            assert command[0] == "pg_restore"
            assert env is None


def test_manifest_write_error_removes_just_created_archive(
    tmp_path, monkeypatch,
):
    _mock_native_dump(monkeypatch)
    destination = tmp_path / "control.dump"
    manifest = tmp_path / "control.sha256.json"
    original_open = backup.os.open

    def fail_manifest_write(path, flags, mode=0o777, **kwargs):
        if str(path) == str(manifest):
            raise OSError("manifest storage unavailable")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(backup.os, "open", fail_manifest_write)
    dsn = "postgresql://worker:local-secret@127.0.0.1:5432/control"
    with pytest.raises(OSError, match="manifest storage unavailable"):
        backup.backup_control(destination, dsn)
    assert not destination.exists()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".*.partial"))


def test_mock_native_dump_generates_private_manifest_without_credentials(
    tmp_path, monkeypatch,
):
    _mock_native_dump(monkeypatch)
    dest = tmp_path / "control.dump"
    result = backup.backup_control(
        dest, "postgresql://worker:local-secret@127.0.0.1:5432/control",
    )
    assert result["format"] == "clashbolt-control-backup-v1"
    assert result["archive_bytes"] == len(b"PGDMPfixed")
    assert result["contains_sensitive_data"] is True
    assert "local-secret" not in json.dumps(result)
    assert dest.read_bytes() == b"PGDMPfixed"
    assert json.loads((tmp_path / "control.sha256.json").read_text()) == result
    assert dest.stat().st_mode & 0o777 == 0o600


def test_restore_refuses_absent_and_corrupt_manifests_without_touching_target(
    tmp_path, monkeypatch,
):
    control = "postgresql://u:p@127.0.0.1:5432/control"
    target = "postgresql://admin:p@127.0.0.1:5433/postgres"
    with pytest.raises(ValueError, match="Archive and SHA-256 manifest"):
        backup.verify_disposable_restore(tmp_path / "absent.dump", control, target)
    archive = tmp_path / "damaged.dump"
    archive.write_bytes(b"PGDMPfake")
    sidecar = tmp_path / "damaged.sha256.json"
    sidecar.write_text(json.dumps({
        "format": "invalid", "archive_bytes": len(archive.read_bytes()),
        "archive_sha256": "0" * 64,
    }))
    monkeypatch.setattr(
        backup.psycopg, "connect",
        lambda *_args, **_kwargs: pytest.fail("No database access for invalid archive"),
    )
    with pytest.raises(ValueError, match="manifest"):
        backup.verify_disposable_restore(archive, control, target)


def test_failed_disposable_restore_drops_only_its_scratch_database(
    tmp_path, monkeypatch,
):
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not control or not admin or not shutil.which("pg_dump"):
        pytest.skip("Two disposable CI PostgreSQL clusters are required")
    Policy(control).migrate()
    archive = tmp_path / "control.dump"
    backup.backup_control(archive, control)
    with psycopg.connect(admin, autocommit=True) as connection:
        before = {
            row[0] for row in connection.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'cb_restore_%'"
            )
        }

    def fail_restore(command, **_kwargs):
        assert command[0] == "pg_restore"
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(backup.subprocess, "run", fail_restore)
    with pytest.raises(RuntimeError, match="Disposable restore could not complete"):
        backup.verify_disposable_restore(archive, control, admin)
    with psycopg.connect(admin, autocommit=True) as connection:
        after = {
            row[0] for row in connection.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'cb_restore_%'"
            )
        }
    assert after == before


def test_partial_manifest_write_can_be_retried(tmp_path, monkeypatch):
    _mock_native_dump(monkeypatch)
    destination = tmp_path / "control.dump"
    dsn = "postgresql://worker:local-secret@127.0.0.1:5432/control"

    def interrupted(_manifest, output, **_kwargs):
        output.write("{")
        raise OSError("storage full")

    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", interrupted)
        with pytest.raises(OSError, match="storage full"):
            backup.backup_control(destination, dsn)
    assert list(tmp_path.iterdir()) == []
    manifest = backup.backup_control(destination, dsn)
    assert json.loads(destination.with_suffix(".sha256.json").read_text()) == manifest


@pytest.mark.parametrize("manifest", [None, [], True, {"archive_bytes": False}])
def test_invalid_restore_manifest_is_an_input_error(tmp_path, manifest):
    archive = tmp_path / "invalid.dump"
    archive.write_bytes(b"PGDMP")
    archive.with_suffix(".sha256.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        backup.verify_disposable_restore(
            archive, "postgresql://u:p@127.0.0.1:5432/control",
            "postgresql://u:p@127.0.0.1:5433/postgres",
        )


def test_restore_rejects_oversized_manifest_before_reading_it(tmp_path):
    archive = tmp_path / "invalid.dump"
    archive.write_bytes(b"PGDMP")
    archive.with_suffix(".sha256.json").write_text(" " * 16_385)
    with pytest.raises(ValueError, match="size limit"):
        backup.verify_disposable_restore(
            archive, "postgresql://u:p@127.0.0.1:5432/control",
            "postgresql://u:p@127.0.0.1:5433/postgres",
        )


def test_backup_preserves_manifest_that_wins_publication_race(tmp_path, monkeypatch):
    _mock_native_dump(monkeypatch)
    destination = tmp_path / "control.dump"
    sidecar = destination.with_suffix(".sha256.json")
    original_open = backup.os.open

    def concurrent_manifest(path, flags, mode=0o777, **kwargs):
        if path == sidecar:
            sidecar.write_text("preexisting manifest")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(backup.os, "open", concurrent_manifest)
    with pytest.raises(FileExistsError):
        backup.backup_control(destination, "postgresql://worker:local-secret@127.0.0.1/control")
    assert not destination.exists()
    assert sidecar.read_text() == "preexisting manifest"


def test_backup_cli_requires_local_database_and_preserves_existing_output(
    tmp_path, monkeypatch, capsys,
):
    import sys

    dest = tmp_path / "existing.dump"
    dest.write_bytes(b"existing archive")
    monkeypatch.setattr(sys, "argv", ["backup", str(dest)])
    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="2"):
        backup.main()
    assert "CLASHBOLT_DATABASE_URL must be configured" in capsys.readouterr().err
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "postgresql://ci:local@localhost/controls")
    with pytest.raises(SystemExit, match="2"):
        backup.main()
    assert "already exists" in capsys.readouterr().err
    assert dest.read_bytes() == b"existing archive"
