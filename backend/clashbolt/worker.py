"""Run a separate, explicit worker process. Never use FastAPI BackgroundTasks."""

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg

from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.store import Ledger
from clashbolt.target_db import disposable_target, drop_abandoned_target


async def isolated_synthetic_run(
    variant: str, schedule: str, target_dsn: str | None = None
) -> dict:
    """Run the allowlisted child with a minimized environment."""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "VIRTUAL_ENV", "SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    if target_dsn is not None:
        environment["CLASHBOLT_TARGET_DATABASE_URL"] = target_dsn
    # Ephemeral working directory; no inherited repository cwd or HOME.
    with tempfile.TemporaryDirectory(prefix="clashbolt-fixed-child-") as workspace:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "clashbolt.fixture_runner", variant, schedule,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env=environment,
            cwd=workspace,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            raise
    if process.returncode != 0 or len(stdout) > 100_000:
        raise RuntimeError("Fixed fixture subprocess failed")
    return json.loads(stdout)


def reconcile_targets(ledger: Ledger) -> int:
    """Remove only abandoned databases belonging to expired/replaced claims."""
    admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
    if not admin:
        return 0
    cleaned = 0
    for instance_id in ledger.claim_abandoned_targets():
        try:
            drop_abandoned_target(admin, ledger.dsn, instance_id)
            ledger.mark_target(instance_id, "CLEANED")
            cleaned += 1
        except (psycopg.Error, OSError, RuntimeError, ValueError):
            ledger.mark_target(instance_id, "UNKNOWN")
    return cleaned


async def process_one(ledger: Ledger, owner: str) -> bool:
    reconcile_targets(ledger)
    ledger.reconcile_expired_cancelled()
    claim = ledger.claim(owner)
    if claim is None:
        return False

    # If cancel raced with claim, the authoritative final transition is UNKNOWN.
    # The fixture is entirely synthetic, newly initialized for every execution.
    try:
        if claim["execution_backend"] == "native-postgresql":
            admin_url = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
            if not admin_url:
                raise ValueError("Native target cluster is not configured")
            run_id = UUID(claim["run_id"])
            generation = claim["lease_generation"]
            instance_id = uuid5(
                NAMESPACE_URL, f"clashbolt:{run_id}:{generation}"
            )
            if not ledger.track_target(run_id, generation, owner, instance_id):
                raise ValueError("Target claim expired before provisioning")
            try:
                with disposable_target(admin_url, ledger.dsn, instance_id) as target:
                    ledger.mark_target(instance_id, "ACTIVE")
                    witness = await isolated_synthetic_run(
                        claim["variant"], claim["schedule"], target.url
                    )
                ledger.mark_target(instance_id, "CLEANED")
            except (psycopg.Error, OSError, RuntimeError, ValueError, TimeoutError):
                ledger.mark_target(instance_id, "UNKNOWN")
                raise
        else:
            witness = await isolated_synthetic_run(claim["variant"], claim["schedule"])
    except (TimeoutError, RuntimeError, OSError, ValueError, psycopg.Error):
        # An ambiguous child outcome is never promoted into a proved finding.
        witness = {
            "variant": claim["variant"],
            "schedule": claim["schedule"],
            "oracle": {"sha256": ORACLE_HASH},
            "complete": False,
            "verdict": "UNKNOWN",
            "failure_reason": "fixture child process did not return valid evidence",
        }
    accepted = ledger.finish(
        UUID(claim["run_id"]), owner, claim["lease_generation"], witness
    )
    return accepted


async def main() -> None:
    parser = argparse.ArgumentParser(description="ClashBolt local PostgreSQL worker")
    parser.add_argument("--once", action="store_true", help="process at most one queued run")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        parser.error("Set CLASHBOLT_DATABASE_URL to a local PostgreSQL database")
    if not 0.1 <= args.interval <= 60:
        parser.error("interval must be 0.1 to 60 seconds")
    ledger = Policy(dsn)
    worker_id = f"worker-{uuid4()}"
    while True:
        ledger.reconcile_stalled_plans()
        accepted = await process_one(ledger, worker_id)
        if args.once:
            break
        if not accepted:
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
