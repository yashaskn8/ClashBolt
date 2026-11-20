"""Run a separate, explicit worker process. Never use FastAPI BackgroundTasks."""

import argparse
import asyncio
import os
from uuid import UUID, uuid4

from clashbolt.experiment import execute
from clashbolt.store import Ledger


async def process_one(ledger: Ledger, owner: str) -> bool:
    ledger.reconcile_expired_cancelled()
    claim = ledger.claim(owner)
    if claim is None:
        return False

    # If cancel raced with claim, the authoritative final transition is UNKNOWN.
    # The fixture is entirely synthetic, newly initialized for every execution.
    witness = await execute(claim["variant"], claim["schedule"])
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
    ledger = Ledger(dsn)
    worker_id = f"worker-{uuid4()}"
    while True:
        accepted = await process_one(ledger, worker_id)
        if args.once:
            break
        if not accepted:
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
