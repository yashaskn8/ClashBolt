"""Qualify acknowledged ledger writes across a disposable synchronous PostgreSQL failover."""

import asyncio
import json
import os
import secrets
import socket
import subprocess
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import UUID, uuid4

import psycopg
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from psycopg.conninfo import make_conninfo

HOST = "127.0.0.1"
ROLE = "clashbolt_failover"
STANDBY = "clashbolt_standby"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        return listener.getsockname()[1]


def stop_cluster(bin_dir: Path, data: Path) -> None:
    command = [str(bin_dir / "pg_ctl"), "-D", str(data)]
    status = subprocess.run([*command, "status"], capture_output=True, timeout=10, check=False)
    if status.returncode != 3:
        subprocess.run([*command, "stop", "-m", "immediate", "-w", "-t", "15"],
                       capture_output=True, timeout=20, check=True)


def require_synchronous_standby(conn: psycopg.Connection) -> None:
    conn.execute("ALTER SYSTEM SET synchronous_standby_names = 'clashbolt_standby'")
    conn.execute("SELECT pg_reload_conf()")
    deadline = monotonic() + 20
    while monotonic() < deadline:
        row = conn.execute(
            "SELECT state, sync_state FROM pg_stat_replication WHERE application_name = %s",
            (STANDBY,),
        ).fetchone()
        if row == ("streaming", "sync"):
            return
        sleep(0.2)
    raise RuntimeError("Standby did not enter synchronous streaming")


def verify_failover(primary: Policy, standby: Policy, failover: Callable[[], None]) -> dict:
    primary.migrate()
    proposal = primary.propose("synchronous-failover-drill")
    approval = primary.approve(UUID(proposal["id"]), ORACLE_HASH, "fixture-reviewer")
    request = {
        "variant": "locked", "schedule": "serial",
        "approved_oracle_id": UUID(approval["id"]),
    }
    claimed_key, pending_key = str(uuid4()), str(uuid4())
    claimed = primary.admit(client_key=claimed_key, **request)
    original = primary.claim("before-failover")
    assert original and original["run_id"] == claimed["run_id"]
    pending = primary.admit(client_key=pending_key, **request)
    run_id, pending_id = (UUID(run["run_id"]) for run in (claimed, pending))
    history = {id_: primary.transitions(id_) for id_ in (run_id, pending_id)}
    assert [event["status"] for event in history[run_id]] == ["PENDING", "RUNNING"]
    assert [event["status"] for event in history[pending_id]] == ["PENDING"]

    started_at = monotonic()
    failover()
    with standby.connect() as conn:
        assert conn.execute("SELECT pg_is_in_recovery() AS standby").fetchone() == {
            "standby": False,
        }
    assert standby.get(run_id) == original
    assert standby.get(pending_id) == pending
    assert {id_: standby.transitions(id_) for id_ in history} == history
    recovery_seconds = round(monotonic() - started_at, 3)

    assert standby.admit(client_key=claimed_key, **request)["run_id"] == str(run_id)
    assert standby.admit(client_key=pending_key, **request)["run_id"] == str(pending_id)
    with standby.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' WHERE id = %s",
            (run_id,),
        )
    witness = asyncio.run(execute("locked", "serial"))
    for id_, previous_generation in ((run_id, original["lease_generation"]), (pending_id, 0)):
        replacement = standby.claim("after-failover")
        assert replacement and replacement["run_id"] == str(id_)
        generation = replacement["lease_generation"]
        assert generation == previous_generation + 1
        assert not standby.finish(id_, "before-failover", previous_generation, witness)
        assert standby.finish(id_, "after-failover", generation, witness)
        assert not standby.finish(id_, "after-failover", generation, witness)
        events = standby.transitions(id_)
        assert events[:-2] == history[id_]
        assert [event["status"] for event in events[-2:]] == ["RUNNING", "COMPLETE"]
        assert standby.get(id_)["verdict"] == "PASS"
    assert standby.claim("after-failover") is None
    return {
        "acknowledged_runs_recovered": 2,
        "duplicate_terminal_receipts_accepted": 0,
        "stale_claim_receipts_accepted": 0,
        "promoted_readback_seconds": recovery_seconds,
    }


def main() -> None:
    if not __debug__:
        raise RuntimeError("Failover qualification requires active assertions")
    bin_dir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    password = secrets.token_urlsafe(24)
    env["PGPASSWORD"] = password

    def native(tool: str, *args, timeout: int = 25) -> None:
        subprocess.run([str(bin_dir / tool), *map(str, args)],
                       env=env, check=True, timeout=timeout)

    with TemporaryDirectory(prefix="clashbolt-failover-") as directory, ExitStack() as cleanup:
        root = Path(directory)
        primary, standby = root / "primary", root / "standby"
        passpath = root / "password"
        passpath.write_text(password + "\n", encoding="ascii")
        native("initdb", "-D", primary, "-U", ROLE, "-A", "scram-sha-256",
               "--pwfile", passpath, "-E", "UTF8", "--no-locale")
        primary_port = free_port()

        def start(data: Path, port: int) -> None:
            with (data / "postgresql.auto.conf").open("a", encoding="ascii") as config:
                config.write(
                    f"\nlisten_addresses = '{HOST}'\nport = {port}\n"
                    "unix_socket_directories = ''\nsynchronous_commit = 'remote_apply'\n"
                    "fsync = on\n"
                )
            cleanup.callback(stop_cluster, bin_dir, data)
            log = root / f"{data.name}.log"
            try:
                native("pg_ctl", "start", "-D", data, "-w", "-t", "15", "-l", log)
            except subprocess.SubprocessError as error:
                details = log.read_text(errors="replace")[-2000:] if log.exists() else ""
                raise RuntimeError(details.replace(password, "[fixture password]")) from error

        start(primary, primary_port)
        native("createdb", "-h", HOST, "-p", primary_port, "-U", ROLE, "--no-password", ROLE)
        primary_dsn = make_conninfo(
            host=HOST, hostaddr=HOST, port=primary_port, dbname=ROLE, user=ROLE,
            password=password, connect_timeout=5,
            options="-c statement_timeout=15000",
        )
        native("pg_basebackup", "-D", standby, "-h", HOST, "-p", primary_port,
               "-U", ROLE, "--no-password", "--wal-method=stream", "--checkpoint=fast",
               timeout=90)
        (standby / "standby.signal").touch()
        with (standby / "postgresql.auto.conf").open("a", encoding="ascii") as config:
            config.write(
                f"\nprimary_conninfo = 'host={HOST} port={primary_port} user={ROLE} "
                f"application_name={STANDBY} password={password}'\n"
            )
        standby_port = free_port()
        start(standby, standby_port)
        standby_dsn = make_conninfo(primary_dsn, port=standby_port)
        with psycopg.connect(primary_dsn, autocommit=True) as conn:
            require_synchronous_standby(conn)
            assert conn.execute("SHOW synchronous_commit").fetchone() == ("remote_apply",)
            assert conn.execute("SHOW fsync").fetchone() == ("on",)

        def promote() -> None:
            native("pg_ctl", "stop", "-D", primary, "-m", "immediate", "-w", "-t", "15")
            native("pg_ctl", "promote", "-D", standby, "-w", "-t", "15")

        print(json.dumps(verify_failover(Policy(primary_dsn), Policy(standby_dsn), promote)))


if __name__ == "__main__":
    main()
