"""Native, non-destructive PostgreSQL control-state backups.

Backups contain sensitive state (including account password hashes). This CLI
does not encrypt, upload, or automatically restore database contents.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

FORMAT = "clashbolt-control-backup-v1"


def _pg_environment(dsn: str) -> dict:
    opts = conninfo_to_dict(dsn)
    host = opts.get("host")
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Backups require an explicit loopback PostgreSQL host")
    if not opts.get("dbname") or not opts.get("user"):
        raise ValueError("Database name and user are required for the backup")
    env = {
        key: val for key, val in os.environ.items()
        if not key.startswith("PG")
    }
    env.update({
        "PGHOST": host,
        "PGPORT": str(opts.get("port", 5432)),
        "PGDATABASE": opts["dbname"],
        "PGUSER": opts["user"],
        "PGCONNECT_TIMEOUT": "5",
    })
    if opts.get("password"):
        env["PGPASSWORD"] = opts["password"]
    return env


def _manifest_path(target: Path) -> Path:
    return target.with_name(target.name + ".manifest.json")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(target: Path) -> dict:
    target = target.resolve()
    sidecar = _manifest_path(target)
    if not target.is_file() or not sidecar.is_file():
        raise ValueError("Dump and manifest must both exist")
    if sidecar.stat().st_size > 16_384:
        raise ValueError("Backup manifest exceeds size limit")
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT
        or manifest.get("size_bytes") != target.stat().st_size
        or manifest.get("sha256") != _sha256_file(target)
    ):
        raise ValueError("Backup size or digest verification failed")
    restore = shutil.which("pg_restore")
    if not restore:
        raise RuntimeError("Install the native PostgreSQL client (pg_restore)")
    # List only: no SQL runs and no database is modified by verification.
    checked = subprocess.run(
        [restore, "--list", str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    if checked.returncode != 0:
        raise ValueError("The native PostgreSQL dump structure is invalid")
    return manifest


def create_backup(dsn: str, target: Path) -> dict:
    target = target.expanduser().resolve()
    sidecar = _manifest_path(target)
    if target.suffix != ".dump" or not target.parent.is_dir():
        raise ValueError("Choose a .dump path inside an existing directory")
    if target.exists() or sidecar.exists():
        raise FileExistsError("Backups are immutable; choose a new filename")
    dump = shutil.which("pg_dump")
    if not dump:
        raise RuntimeError("Install the native PostgreSQL client (pg_dump)")
    pg_env = _pg_environment(dsn)
    temporary = tempfile.NamedTemporaryFile(
        prefix=".clashbolt-backup-", suffix=".partial",
        dir=target.parent, delete=False,
    )
    temp_path = Path(temporary.name)
    temporary.close()
    try:
        command = [
            dump, "--format=custom", "--no-owner", "--no-acl",
            "--file", str(temp_path),
        ]
        result = subprocess.run(
            command, env=pg_env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120, check=False,
        )
        if result.returncode != 0 or temp_path.stat().st_size == 0:
            raise RuntimeError("Native PostgreSQL backup failed")
        manifest = {
            "format": FORMAT,
            "scope": "LOCAL_CONTROL_DATABASE",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "size_bytes": temp_path.stat().st_size,
            "sha256": _sha256_file(temp_path),
            "includes_secrets": True,
        }
        # Hard-linking is exclusive: concurrent jobs cannot replace an old dump.
        os.link(temp_path, target)
        sidecar_tmp = tempfile.NamedTemporaryFile(
            mode="w", prefix=".clashbolt-manifest-", suffix=".partial",
            dir=target.parent, delete=False, encoding="utf-8",
        )
        try:
            json.dump(manifest, sidecar_tmp, sort_keys=True)
            sidecar_tmp.flush()
            os.fsync(sidecar_tmp.fileno())
        finally:
            sidecar_tmp.close()
        try:
            os.link(sidecar_tmp.name, sidecar)
        finally:
            Path(sidecar_tmp.name).unlink(missing_ok=True)
        return verify_backup(target)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local PostgreSQL control-state backup")
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            dsn = os.getenv("CLASHBOLT_DATABASE_URL")
            if not dsn:
                parser.error("Set CLASHBOLT_DATABASE_URL before creating a backup")
            result = create_backup(dsn, args.path)
        else:
            result = verify_backup(args.path)
    except (ValueError, RuntimeError, FileExistsError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
