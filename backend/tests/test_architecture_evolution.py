"""Behavioral verification for architectural upgrades in Batch 1.

Covers startup connection parameters, single-pass operations aggregation,
generation- and clock-fenced lease renewal, target lifecycle latency tracking,
and graceful worker shutdown.
"""

import ast
import asyncio
import inspect
import math
import os
import signal
import sys
from contextlib import suppress
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt import worker
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.verification_jobs import VerificationJobs
from tests.test_verifier_crash_recovery import CRASHING_VERIFIER


def _clean_policy_ledger(control_dsn: str) -> Policy:
    ledger = Policy(control_dsn)
    ledger.migrate()
    with ledger.connect() as conn:
        assert conn.info.dbname == "clashbolt_test"
        conn.execute(
            "TRUNCATE witness_export, witness_review, verification_target_instance, "
            "verification_job, target_instance, tool_audit, agent_plan, tool_capability, "
            "oracle_proposal, run_reduction, run_transition, experiment_run, project_quota CASCADE"
        )
    return ledger


def test_ledger_connect_sets_timeouts_and_cancels_slow_query():
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not control:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    ledger = _clean_policy_ledger(control)
    with ledger.connect() as conn:
        assert conn.execute("SHOW search_path").fetchone()["search_path"] == "public"
        assert (
            conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
            == "30s"
        )
        assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "10s"

    with pytest.raises(psycopg.errors.QueryCanceled):
        with ledger.connect() as conn:
            conn.execute("SET LOCAL statement_timeout = '50ms'")
            conn.execute("SELECT pg_sleep(0.5)")


def test_operations_single_pass_aggregation_parity():
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not control:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    ledger = _clean_policy_ledger(control)
    proposal = ledger.propose("ops-parity")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    oid = UUID(approved["id"])

    run_pending = ledger.admit(
        client_key="ops-pending",
        variant="racy",
        schedule="overlap",
        approved_oracle_id=oid,
        reserved_cpu_ms=2000,
    )
    run_running = ledger.admit(
        client_key="ops-running",
        variant="locked",
        schedule="serial",
        approved_oracle_id=oid,
        execution_backend="memory",
        reserved_cpu_ms=4000,
    )
    assert run_pending["status"] == "PENDING"
    assert run_running["status"] == "PENDING"

    with ledger.connect() as conn:
        # Promote run_running to RUNNING with active native slot if target configured.
        conn.execute(
            """
            UPDATE experiment_run
            SET status = 'RUNNING', version = 2, lease_generation = 1,
                lease_owner = 'w-1', lease_until = now() + interval '30 seconds',
                execution_backend = 'native-postgresql', reserved_target_dbs = 1
            WHERE id = %s
            """,
            (UUID(run_running["run_id"]),),
        )
        # Seed a COMPLETED overspent row with measured metrics directly via INSERT.
        overspent_id = uuid4()
        conn.execute(
            """
            INSERT INTO experiment_run(
                id, project_id, client_key, request_hash, variant, schedule,
                oracle_hash, step_budget, approved_oracle_id, execution_backend,
                reserved_cpu_ms, reserved_target_dbs, status, version, verdict,
                accounting_state, measured_worker_cpu_ms, measured_app_wall_ms,
                resource_accounting
            ) VALUES (
                %s, 'local', 'ops-complete', 'hash-ops-complete', 'locked', 'serial',
                %s, 4, %s, 'memory', 1000, 0, 'COMPLETE', 3, 'PASS',
                'RECONCILED', 25.5, 80.25,
                '{"reconciliation": {"status": "RECONCILED", "overspent": true}}'::jsonb
            )
            """,
            (overspent_id, ORACLE_HASH, oid),
        )

    snap = ledger.operations()
    assert snap["runs"] == {"PENDING": 1, "RUNNING": 1, "COMPLETE": 1}
    assert snap["native_slots_occupied"] == 1
    assert snap["resources"]["open_runs"] == 2
    assert snap["resources"]["open_reserved_cpu_ms"] == 6000
    assert snap["resources"]["open_reserved_target_dbs"] == 1
    assert snap["resources"]["runs_7d"]["total_runs"] == 3
    assert snap["resources"]["runs_7d"]["overspent_count"] == 1
    assert snap["resources"]["runs_7d"]["overspend_rate"] == 1.0
    assert snap["resources"]["runs_7d"]["measured_worker_cpu_ms_total"] == 25.5
    assert snap["resources"]["runs_7d"]["measured_app_wall_ms_total"] == 80.25
    assert snap["cancellation"]["in_flight"] == 0


def test_ledger_renew_lease_fences_stale_expired_and_terminal():
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not control:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    ledger = _clean_policy_ledger(control)
    proposal = ledger.propose("lease-fence")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()),
        variant="locked",
        schedule="serial",
        approved_oracle_id=UUID(approved["id"]),
    )
    run_id = UUID(run["run_id"])
    claim = ledger.claim("worker-a", lease_seconds=10)
    assert claim is not None and claim["lease_generation"] == 1

    with pytest.raises(ValueError):
        ledger.renew_lease(run_id, "", 1, 20)
    with pytest.raises(ValueError):
        ledger.renew_lease(run_id, "worker-a", 0, 20)
    with pytest.raises(ValueError):
        ledger.renew_lease(run_id, "worker-a", 1, 4)

    assert ledger.renew_lease(run_id, "worker-b", 1, 20) is False
    assert ledger.renew_lease(run_id, "worker-a", 2, 20) is False

    with ledger.connect() as conn:
        before = conn.execute(
            "SELECT lease_until, lease_generation FROM experiment_run WHERE id = %s",
            (run_id,),
        ).fetchone()

    assert ledger.renew_lease(run_id, "worker-a", 1, 120) is True

    with ledger.connect() as conn:
        after = conn.execute(
            "SELECT lease_until, lease_generation FROM experiment_run WHERE id = %s",
            (run_id,),
        ).fetchone()
        assert after["lease_generation"] == before["lease_generation"] == 1
        assert after["lease_until"] > before["lease_until"]
        # Expired clock check
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' WHERE id = %s",
            (run_id,),
        )

    assert ledger.renew_lease(run_id, "worker-a", 1, 30) is False

    # Restore active lease, then move run to terminal COMPLETE -> renewal must fail.
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET status = 'COMPLETE', "
            "lease_until = now() + interval '30 seconds' WHERE id = %s",
            (run_id,),
        )
    assert ledger.renew_lease(run_id, "worker-a", 1, 30) is False


def test_verification_jobs_renew_lease_and_target_latency_persistence():
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not control:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    ledger = _clean_policy_ledger(control)
    proposal = ledger.propose("verify-lease")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()),
        variant="locked",
        schedule="serial",
        approved_oracle_id=UUID(approved["id"]),
    )
    assert asyncio.run(worker.process_one(ledger, "fixture-worker")) is True

    queue = VerificationJobs(control)
    submitted = queue.submit(UUID(run["run_id"]), "REPLAY")
    job_id = UUID(submitted["job_id"])
    claim = queue.claim("verifier-a", lease_seconds=10)
    assert claim is not None and claim["lease_generation"] == 1

    with pytest.raises(ValueError):
        queue.renew_lease(job_id, "", 1, 20)
    with pytest.raises(ValueError):
        queue.renew_lease(job_id, "verifier-a", 0, 20)
    with pytest.raises(ValueError):
        queue.renew_lease(job_id, "verifier-a", 1, 301)

    assert queue.renew_lease(job_id, "verifier-b", 1, 20) is False
    assert queue.renew_lease(job_id, "verifier-a", 2, 20) is False
    assert queue.renew_lease(job_id, "verifier-a", 1, 60) is True

    instance_id = uuid4()
    assert queue.track_target(job_id, "verifier-a", 1, instance_id) is True

    with pytest.raises(ValueError):
        queue.mark_target(instance_id, "ACTIVE", provision_ms=-0.1)
    with pytest.raises(ValueError):
        queue.mark_target(instance_id, "ACTIVE", teardown_ms=math.nan)

    queue.mark_target(instance_id, "ACTIVE", provision_ms=18.25)
    queue.mark_target(instance_id, "CLEANED", teardown_ms=7.5)
    with ledger.connect() as conn:
        row = conn.execute(
            "SELECT status, provision_ms, teardown_ms "
            "FROM verification_target_instance WHERE id = %s",
            (instance_id,),
        ).fetchone()
    assert row["status"] == "CLEANED"
    assert row["provision_ms"] == 18.25
    assert row["teardown_ms"] == 7.5

    with ledger.connect() as conn:
        conn.execute(
            "UPDATE verification_job SET lease_until = now() - interval '1 second' WHERE id = %s",
            (job_id,),
        )
    assert queue.renew_lease(job_id, "verifier-a", 1, 30) is False


def test_crashing_verifier_wrapper_forwards_latency_kwargs():
    parsed = ast.parse(CRASHING_VERIFIER)
    fn_node = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "stop_after_active"
    )
    recorded: list[tuple[tuple, dict]] = []

    def stub_original(self, instance_id, status, *args, **kwargs):
        recorded.append(((self, instance_id, status, *args), kwargs))

    namespace = {"original": stub_original, "os": os}
    module = ast.Module(body=[fn_node], type_ignores=[])
    exec(compile(module, "CRASHING_VERIFIER", "exec"), namespace)
    target_id = uuid4()
    stub_self = object()
    namespace["stop_after_active"](
        stub_self, target_id, "CLEANED", provision_ms=11.5, teardown_ms=3.25
    )
    assert recorded == [
        ((stub_self, target_id, "CLEANED"), {"provision_ms": 11.5, "teardown_ms": 3.25})
    ]
    params = inspect.signature(VerificationJobs.mark_target).parameters
    assert {"provision_ms", "teardown_ms"} <= set(params)


def test_keep_lease_alive_renews_periodically_and_stops_on_false():
    async def scenario() -> None:
        calls: list[int] = []

        def flaky_then_false() -> bool:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise psycopg.OperationalError("transient error")
            if len(calls) >= 3:
                return False
            return True

        async with worker._keep_lease_alive(flaky_then_false, interval_seconds=0.02):
            await asyncio.sleep(0.15)
        assert len(calls) == 3

        calls_active: list[int] = []

        def always_true() -> bool:
            calls_active.append(1)
            return True

        async with worker._keep_lease_alive(always_true, interval_seconds=0.02):
            await asyncio.sleep(0.08)
        count_at_exit = len(calls_active)
        assert count_at_exit >= 1
        await asyncio.sleep(0.06)
        assert len(calls_active) == count_at_exit

    asyncio.run(scenario())


def test_worker_main_graceful_stop_drains_cleanly(monkeypatch):
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not control:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    ledger = _clean_policy_ledger(control)
    proposal = ledger.propose("graceful-stop")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    run = ledger.admit(
        client_key=str(uuid4()),
        variant="locked",
        schedule="serial",
        approved_oracle_id=UUID(approved["id"]),
    )
    run_id = UUID(run["run_id"])

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", control)
    monkeypatch.delenv("CLASHBOLT_ACCESS_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["clashbolt-worker", "--interval", "0.1"])

    previous_sigint = signal.getsignal(signal.SIGINT)

    async def scenario() -> None:
        stop_event = asyncio.Event()

        async def stop_when_done() -> None:
            for _ in range(100):
                current = ledger.get(run_id)
                if current and current["status"] == "COMPLETE":
                    stop_event.set()
                    return
                await asyncio.sleep(0.05)
            stop_event.set()

        stopper = asyncio.create_task(stop_when_done())
        try:
            await asyncio.wait_for(worker.main(stop_event=stop_event), timeout=10.0)
        finally:
            if not stopper.done():
                stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper

    asyncio.run(scenario())
    assert signal.getsignal(signal.SIGINT) == previous_sigint
    finished = ledger.get(run_id)
    assert finished is not None
    assert finished["status"] == "COMPLETE"
    assert finished["verdict"] == "PASS"
    with ledger.connect() as conn:
        active_targets = conn.execute(
            "SELECT count(*) AS n FROM target_instance WHERE status IN ('PROVISIONING', 'ACTIVE')"
        ).fetchone()["n"]
    assert active_targets == 0
