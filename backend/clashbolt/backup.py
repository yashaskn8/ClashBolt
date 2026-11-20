"""Native local PostgreSQL control-plane backup with integrity verification.

Creates a new private archive only. Never restores, deletes or overwrites
an existing archive or a live database.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashbolt.target_db import validate_target_cluster


def _loopback(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _database_args(dsn: str) -> tuple[list[str], str]:
    try:
        config = conninfo_to_dict(dsn)
    except ValueError as error:
        raise ValueError("Invalid PostgreSQL connection string") from error
    if (
        not _loopback(config.get("host", ""))
        or (
            config.get("hostaddr")
            and not _loopback(config["hostaddr"])
        )
        or not config.get("dbname")
        or not config.get("user")
        or not config.get("password")
    ):
        raise ValueError("A password-protected loopback PostgreSQL DSN is required")
    database = config["dbname"]
    if database in ("postgres", "template0", "template1"):
        raise ValueError("Back up the dedicated ClashBolt database, not a system database")
    command = [
        "--no-password", "--host", config["host"],
        "--port", str(config.get("port", "5432")),
        "--username", config["user"], "--dbname", database,
    ]
    return command, config["password"]


def backup_control(destination: Path, dsn: str) -> dict:
    """Create and fully read-verify a custom archive without password argv leaks."""
    args, password = _database_args(dsn)
    destination = destination.expanduser().resolve()
    if destination.suffix != ".dump":
        raise ValueError("Backup destination must have a .dump extension")
    if not destination.parent.is_dir():
        raise ValueError("Backup destination directory does not exist")
    manifest_path = destination.with_suffix(".sha256.json")
    if destination.exists() or manifest_path.exists():
        raise FileExistsError("Backup destination or manifest already exists")

    environment = os.environ.copy()
    for key in (
        "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER",
        "PGSERVICE", "PGPASSFILE", "PGOPTIONS", "PGPASSWORD",
    ):
        environment.pop(key, None)
    environment["PGPASSWORD"] = password
    descriptor, temporary = tempfile.mkstemp(
        prefix=".clashbolt-backup-", suffix=".partial", dir=destination.parent
    )
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    try:
        try:
            subprocess.run(
                ["pg_dump", "--format=custom", f"--file={temporary}", *args],
                env=environment, check=True, capture_output=True, timeout=120,
            )
            subprocess.run(
                ["pg_restore", "--list", temporary],
                check=True, capture_output=True, timeout=30,
            )
            # Decompress and read *all* archive contents without touching a DB.
            subprocess.run(
                ["pg_restore", f"--file={os.devnull}", temporary],
                check=True, capture_output=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Native PostgreSQL backup or integrity check failed") from error
        data = Path(temporary).read_bytes()
        if not data.startswith(b"PGDMP"):
            raise ValueError("Backup does not contain a PostgreSQL custom archive")
        manifest = {
            "format": "clashbolt-control-backup-v1",
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "archive_bytes": len(data),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "restore_status": "NOT_ATTEMPTED",
            "contains_sensitive_data": True,
        }
        # Hard links create the final archive atomically and refuse overwrite.
        os.link(temporary, destination)
        try:
            fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(manifest, output, sort_keys=True, indent=2)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return manifest
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_disposable_restore(
    archive: Path, control_dsn: str, target_admin_dsn: str
) -> dict:
    """Restore only into a generated *new* local database, then force-remove it.

    No restore into any caller-selected existing database is supported.
    The administrator credential remains confined to this trusted CLI/test.
    """
    validate_target_cluster(target_admin_dsn, control_dsn)
    archive = archive.expanduser().resolve()
    manifest_path = archive.with_suffix(".sha256.json")
    if not archive.is_file() or not manifest_path.is_file():
        raise ValueError("Archive and SHA-256 manifest are both required")
    if archive.stat().st_size > 100_000_000:
        raise ValueError("Restore drills are limited to 100 MB archives")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("format") != "clashbolt-control-backup-v1"
        or manifest.get("archive_bytes") != archive.stat().st_size
        or manifest.get("archive_sha256")
            != hashlib.sha256(archive.read_bytes()).hexdigest()
    ):
        raise ValueError("Archive does not match the expected backup manifest")

    config = conninfo_to_dict(target_admin_dsn)
    if not config.get("user") or not config.get("password"):
        raise ValueError("A dedicated native target administrator is required")
    scratch = f"cb_restore_{uuid4().hex}"
    environment = os.environ.copy()
    for key in (
        "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER",
        "PGSERVICE", "PGPASSFILE", "PGOPTIONS", "PGPASSWORD",
    ):
        environment.pop(key, None)
    environment["PGPASSWORD"] = config["password"]

    with psycopg.connect(
        target_admin_dsn, autocommit=True, connect_timeout=5
    ) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch))
        )
        try:
            try:
                subprocess.run(
                    [
                        "pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                        "--no-password", "--host", config["host"],
                        "--port", str(config.get("port", "5432")),
                        "--username", config["user"], "--dbname", scratch,
                        str(archive),
                    ],
                    env=environment, check=True, capture_output=True, timeout=120,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError("Disposable restore could not complete") from error
            with psycopg.connect(
                make_conninfo(target_admin_dsn, dbname=scratch),
                connect_timeout=5,
            ) as restored:
                rows = restored.execute(
                    "SELECT component, version FROM schema_revision"
                ).fetchall()
                revisions = {row[0]: row[1] for row in rows}
                if not {"control", "policy"}.issubset(revisions):
                    raise ValueError("Restored control/policy schema revisions missing")
            return {
                "status": "RESTORE_VERIFIED",
                "restored_schema_revisions": revisions,
                "scratch_database": scratch,
                "scope": "DISPOSABLE_NATIVE_CLUSTER_ONLY",
            }
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(scratch)
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a private, verified native ClashBolt database archive"
    )
    parser.add_argument("destination", type=Path, help="New .dump file in an existing directory")
    args = parser.parse_args()
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        parser.error("CLASHBOLT_DATABASE_URL must be configured")
    try:
        result = backup_control(args.destination, dsn)
    except (ValueError, FileExistsError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
