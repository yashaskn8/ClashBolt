"""Run a separate, explicit worker process. Never use FastAPI BackgroundTasks."""

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

import psycopg

from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.store import Ledger
from clashbolt.target_db import disposable_target


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
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "clashbolt.fixture_runner", variant, schedule,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0 or len(stdout) > 100_000:
        raise RuntimeError("Fixed fixture subprocess failed")
    return json.loads(stdout)


async def process_one(ledger: Ledger, owner: str) -> bool:
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
            with disposable_target(admin_url, ledger.dsn, UUID(claim["run_id"])) as target:
                witness = await isolated_synthetic_run(
                    claim["variant"], claim["schedule"], target.url
                )
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
