"""Run a separate, explicit worker process. Never use FastAPI BackgroundTasks."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from itertools import count
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg

from clashbolt.accounts import AccountStore
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.reducer import reduce_witness
from clashbolt.store import Ledger
from clashbolt.target_db import disposable_target, drop_abandoned_target
from clashbolt.target_service import request_fixture, service_enabled
from clashbolt.verification_jobs import VerificationJobs
from clashbolt.witness import validate_contract, verify

logger = logging.getLogger("clashbolt.worker")


def _log_event(event: str, **fields: object) -> None:
    """Emit structured, secret-free worker lifecycle telemetry."""
    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))


@asynccontextmanager
async def _keep_lease_alive(renew_fn, interval_seconds: float = 10.0):
    """Renew an active claim periodically while in-flight work executes."""
    stop = asyncio.Event()

    async def pump() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                pass
            try:
                kept = await asyncio.to_thread(renew_fn)
            except psycopg.Error as error:
                _log_event("lease_renewal_error", error=type(error).__name__)
                continue
            if not kept:
                _log_event("lease_renewal_stopped")
                return

    task = asyncio.create_task(pump())
    try:
        yield
    finally:
        stop.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _child_cpu_seconds() -> float | None:
    if os.name != "posix":
        return None
    import resource

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


async def isolated_synthetic_run(
    variant: str, schedule: str, target_dsn: str | None = None
) -> dict:
    """Run the allowlisted child with a minimized environment."""
    if service_enabled():
        evidence = await asyncio.to_thread(request_fixture, variant, schedule, target_dsn)
        if validate_contract(evidence) is not None:
            raise ValueError("Target service returned invalid evidence")
        return evidence
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
        async def bounded_output() -> bytes:
            output = bytearray()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 100_000:
                    raise ValueError("Fixed fixture subprocess output exceeded 100 KB")
            await process.wait()
            return bytes(output)

        try:
            stdout = await asyncio.wait_for(bounded_output(), timeout=5)
        except (TimeoutError, ValueError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass

            # asyncio.Process.wait() may hang when a PIPE is still full.
            # After terminating the producer, discard rather than accumulate
            # any unread output, then reap the process with a cleanup deadline.
            async def discard_and_reap() -> None:
                if process.stdout is not None:
                    while await process.stdout.read(8192):
                        pass
                await process.wait()

            await asyncio.wait_for(discard_and_reap(), timeout=2)
            raise
    if process.returncode != 0:
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


def reconcile_verification_targets(queue: VerificationJobs, ledger: Ledger) -> int:
    """Reclaim abandoned native replay/reduction databases from expired claims."""
    admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
    if not admin:
        return 0
    cleaned = 0
    for instance_id in queue.claim_abandoned_targets():
        try:
            drop_abandoned_target(admin, ledger.dsn, instance_id)
            queue.mark_target(instance_id, "CLEANED")
            cleaned += 1
        except (psycopg.Error, OSError, RuntimeError, ValueError):
            queue.mark_target(instance_id, "UNKNOWN")
    return cleaned


async def process_one(ledger: Ledger, owner: str) -> bool:
    reconcile_targets(ledger)
    ledger.reconcile_expired_cancelled()
    claim = ledger.claim(owner)
    if claim is None:
        return False

    # If cancel raced with claim, the authoritative final transition is UNKNOWN.
    # The fixture is entirely synthetic, newly initialized for every execution.
    wall_start = time.perf_counter()
    worker_cpu_start = time.process_time()
    child_cpu_start = None if service_enabled() else _child_cpu_seconds()
    db_stats: dict | None = None
    run_uuid = UUID(claim["run_id"])
    generation = claim["lease_generation"]
    async with _keep_lease_alive(
        lambda: ledger.renew_lease(run_uuid, owner, generation)
    ):
        try:
            if claim["execution_backend"] == "native-postgresql":
                admin_url = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
                if not admin_url:
                    raise ValueError("Native target cluster is not configured")
                instance_id = uuid5(
                    NAMESPACE_URL, f"clashbolt:{run_uuid}:{generation}"
                )
                if not ledger.track_target(run_uuid, generation, owner, instance_id):
                    raise ValueError("Target claim expired before provisioning")
                try:
                    with disposable_target(admin_url, ledger.dsn, instance_id) as target:
                        provision_ms = target.provision_ms
                        ledger.mark_target(
                            instance_id, "ACTIVE", provision_ms=provision_ms
                        )
                        witness = await isolated_synthetic_run(
                            claim["variant"], claim["schedule"], target.url
                        )
                        db_stats = target.sample_stats()
                        teardown_start = time.perf_counter()
                    teardown_ms = round(
                        max(0.0, (time.perf_counter() - teardown_start) * 1000), 3
                    )
                    ledger.mark_target(
                        instance_id, "CLEANED", teardown_ms=teardown_ms
                    )
                    if db_stats is not None:
                        db_stats["provision_ms"] = provision_ms
                        db_stats["teardown_ms"] = teardown_ms
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
    wall_ms = round(max(0.0, (time.perf_counter() - wall_start) * 1000), 3)
    worker_cpu_ms = round(max(0.0, (time.process_time() - worker_cpu_start) * 1000), 3)
    child_cpu_end = None if child_cpu_start is None else _child_cpu_seconds()
    child_cpu_ms = (
        round(max(0.0, (child_cpu_end - child_cpu_start) * 1000), 3)
        if child_cpu_start is not None and child_cpu_end is not None
        else None
    )
    measurements = {
        "worker_cpu_ms": worker_cpu_ms,
        "child_cpu_ms": child_cpu_ms,
        "wall_ms": wall_ms,
        "database": db_stats,
    }
    accepted = ledger.finish(
        run_uuid, owner, generation, witness,
        measurements=measurements,
    )
    _log_event(
        "run_processed",
        project_id=ledger.project_id,
        run_id=claim["run_id"],
        accepted=accepted,
        backend=claim["execution_backend"],
        wall_ms=wall_ms,
    )
    return accepted


async def process_verification_one(ledger: Ledger, owner: str) -> bool:
    """A separate worker executes verification; the API never needs admin DSNs."""
    queue = VerificationJobs(
        ledger.dsn, schema=ledger.schema, project_id=ledger.project_id
    )
    reconcile_verification_targets(queue, ledger)
    claim = queue.claim(owner)
    if claim is None:
        return False

    job_id = UUID(claim["job_id"])
    generation = claim["lease_generation"]
    trial_numbers = count(1)

    @contextmanager
    def tracked_target():
        """Record each target before creating it, including reduction trial targets."""
        admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
        if not admin:
            raise ValueError("Native target cluster is not configured")
        trial = next(trial_numbers)
        if trial > 17:
            raise ValueError("Verification target trial budget exceeded")
        instance_id = uuid5(
            NAMESPACE_URL, f"clashbolt:verification:{job_id}:{generation}:{trial}"
        )
        if not queue.track_target(job_id, owner, generation, instance_id):
            raise ValueError("Verification claim expired before target provisioning")
        try:
            with disposable_target(admin, ledger.dsn, instance_id) as target:
                queue.mark_target(
                    instance_id, "ACTIVE", provision_ms=target.provision_ms
                )
                yield target
                teardown_start = time.perf_counter()
            teardown_ms = round(
                max(0.0, (time.perf_counter() - teardown_start) * 1000), 3
            )
            queue.mark_target(instance_id, "CLEANED", teardown_ms=teardown_ms)
        except Exception:
            queue.mark_target(instance_id, "UNKNOWN")
            raise

    original = ledger.get(UUID(claim["run_id"]))
    report = {"status": "UNCONFIRMED", "reason": "Run is not currently certified"}
    async with _keep_lease_alive(lambda: queue.renew_lease(job_id, owner, generation)):
        if (
            original and original["status"] == "COMPLETE"
            and original["version"] == claim["run_version"]
            and original["witness"] is not None
        ):
            try:
                if claim["kind"] == "REPLAY":
                    report = await verify(original["witness"], tracked_target)
                else:
                    report = await asyncio.to_thread(
                        reduce_witness,
                        original["witness"],
                        os.getenv("CLASHBOLT_TARGET_ADMIN_URL"),
                        ledger.dsn,
                        tracked_target,
                    )
            except (TimeoutError, RuntimeError, OSError, ValueError, psycopg.Error):
                report = {"status": "UNCONFIRMED", "reason": "Verification worker failed"}
    accepted = queue.finish(job_id, owner, generation, report)
    _log_event(
        "verification_processed",
        project_id=ledger.project_id,
        job_id=claim["job_id"],
        kind=claim["kind"],
        accepted=accepted,
        status=report.get("status"),
    )
    return True


async def main(stop_event: asyncio.Event | None = None) -> None:
    parser = argparse.ArgumentParser(description="ClashBolt local PostgreSQL worker")
    parser.add_argument("--once", action="store_true", help="process at most one queued run")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        parser.error("Set CLASHBOLT_DATABASE_URL to a local PostgreSQL database")
    if not 0.1 <= args.interval <= 60:
        parser.error("interval must be 0.1 to 60 seconds")
    registry = AccountStore(dsn) if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts" else None
    worker_id = f"worker-{uuid4()}"
    shutdown = stop_event or asyncio.Event()
    previous_handlers: list[tuple[signal.Signals, object]] = []

    if threading.current_thread() is threading.main_thread():
        def _request_shutdown(_signum, _frame) -> None:
            shutdown.set()
            _log_event("worker_shutdown_requested", worker_id=worker_id)

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                previous_handlers.append((sig, signal.getsignal(sig)))
                signal.signal(sig, _request_shutdown)

    next_project = 0
    database_failures = 0
    try:
        while not shutdown.is_set():
            try:
                # A trusted registry discovers new workspaces without a restart.
                # Rotate the starting project to avoid permanent local-first starvation.
                projects = (
                    registry.worker_projects() if registry is not None
                    else [{"project_id": "local", "schema_name": "public"}]
                )
                if not projects:
                    raise RuntimeError("No initialized worker project schemas")
                start = next_project % len(projects)
                next_project += 1
                ordered = projects[start:] + projects[:start]
                accepted = False
                checked = False
                for offset, project in enumerate(ordered):
                    if shutdown.is_set():
                        break
                    ledger = Policy(
                        dsn, schema=project["schema_name"], project_id=project["project_id"]
                    )
                    ledger.heartbeat(worker_id)
                    ledger.reconcile_corrupt_plans()
                    ledger.reconcile_stalled_plans()
                    ledger.reconcile_stalled_model_calls()
                    # Process at most one execution and one verification per cycle;
                    # all work remains lease-fenced in its own project schema.
                    if not accepted:
                        accepted = await process_one(ledger, worker_id)
                        if accepted:
                            # Idle projects must not give the next busy project extra turns.
                            next_project = start + offset + 1
                    if not checked and (not args.once or not accepted):
                        checked = await process_verification_one(ledger, worker_id)
            except (psycopg.errors.DeadlockDetected, psycopg.OperationalError) as error:
                # Start a fresh cycle. Ambiguous commits stay governed by the
                # persisted generations and leases on the next read.
                database_failures += 1
                if args.once or database_failures >= 3:
                    message = (
                        "Worker database deadlock persisted"
                        if isinstance(error, psycopg.errors.DeadlockDetected)
                        else "Worker database connection failed"
                    )
                    raise RuntimeError(message) from error
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=args.interval)
                except TimeoutError:
                    pass
                continue
            database_failures = 0
            if args.once or shutdown.is_set():
                break
            if not accepted and not checked:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=args.interval)
                except TimeoutError:
                    pass
    finally:
        for sig, handler in previous_handlers:
            signal.signal(sig, handler)


if __name__ == "__main__":
    asyncio.run(main())
