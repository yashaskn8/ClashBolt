"""B7/P5 resource reservation, reconciliation, overspend, and cancel latency tests."""

import asyncio
import math
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.accounting import (
    DEFAULT_RESERVED_CPU_MS,
    build_reservation,
    reconcile_run_accounting,
    released_run_accounting,
    uncertain_run_accounting,
)
from clashbolt.accounts import AccountStore
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.metrics import operational_metrics
from clashbolt.ops_alerts import diagnose_operations
from clashbolt.policy import Policy
from clashbolt.worker import process_one
from fastapi.testclient import TestClient


def test_pure_reconciliation_distinguishes_measured_partial_and_unavailable():
    reserved = build_reservation(
        execution_backend="native-postgresql", step_budget=4, reserved_cpu_ms=3000
    )
    assert reserved["reconciliation"]["status"] == "RESERVED"
    assert reserved["reconciliation"]["overspent"] is None
    assert reserved["estimated"]["provider_cost_usd"] is None
    assert reserved["measured"]["database"]["status"] == "UNAVAILABLE"

    released = released_run_accounting(
        execution_backend="memory", step_budget=4, reserved_cpu_ms=3000
    )
    assert released["reconciliation"]["status"] == "RELEASED"
    assert released["reconciliation"]["overspent"] is False
    assert released["measured"]["process"]["worker_cpu_ms"] is None

    uncertain = uncertain_run_accounting(
        execution_backend="native-postgresql",
        step_budget=4,
        reserved_cpu_ms=3000,
        reason="lease expired",
    )
    assert uncertain["reconciliation"]["status"] == "UNCERTAIN"
    assert uncertain["reconciliation"]["overspent"] is None
    assert uncertain["reconciliation"]["uncertainty_reason"] == "lease expired"

    # Invalid inputs (bool, NaN, negative) never collapse to zero.
    state, partial = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="COMPLETE",
        cancel_requested=False,
        valid_witness=True,
        consumed_steps=4,
        measurements={
            "worker_cpu_ms": True,
            "child_cpu_ms": math.nan,
            "wall_ms": -5.0,
        },
    )
    assert state == "PARTIAL"
    assert partial["measured"]["process"]["worker_cpu_ms"] is None
    assert partial["measured"]["process"]["child_cpu_ms"] is None
    assert partial["measured"]["process"]["status"] == "UNAVAILABLE"
    assert partial["measured"]["application"]["wall_ms"] is None
    assert partial["measured"]["application"]["status"] == "PARTIAL"
    assert partial["reconciliation"]["overspent"] is None

    # App wall only + missing steps -> application PARTIAL.
    _, partial_wall_only = reconcile_run_accounting(
        execution_backend="native-postgresql",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="COMPLETE",
        cancel_requested=False,
        valid_witness=False,
        consumed_steps=None,
        measurements={
            "worker_cpu_ms": 10.0,
            "child_cpu_ms": 2.0,
            "wall_ms": 15.0,
            "database": None,
        },
    )
    assert partial_wall_only["measured"]["application"]["status"] == "PARTIAL"
    assert partial_wall_only["measured"]["database"]["status"] == "UNAVAILABLE"

    # Cancelled + invalid witness + unknown status uncertainty reasons.
    _, cancel_uncertain = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="UNKNOWN",
        cancel_requested=True,
        valid_witness=False,
        consumed_steps=None,
        measurements={},
    )
    assert "Cancelled during execution" in cancel_uncertain["reconciliation"]["uncertainty_reason"]

    _, bad_witness_uncertain = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="UNKNOWN",
        cancel_requested=False,
        valid_witness=False,
        consumed_steps=None,
        measurements={},
    )
    assert "Valid witness missing" in bad_witness_uncertain["reconciliation"]["uncertainty_reason"]

    _, unknown_uncertain = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="UNKNOWN",
        cancel_requested=False,
        valid_witness=True,
        consumed_steps=4,
        measurements={},
    )
    assert "Run ended UNKNOWN" in unknown_uncertain["reconciliation"]["uncertainty_reason"]

    state_ok, reconciled = reconcile_run_accounting(
        execution_backend="native-postgresql",
        step_budget=4,
        reserved_cpu_ms=3000,
        terminal_status="COMPLETE",
        cancel_requested=False,
        valid_witness=True,
        consumed_steps=4,
        measurements={
            "worker_cpu_ms": 12.5,
            "child_cpu_ms": 7.5,
            "wall_ms": 40.0,
            "database": {
                "active_ms": 4.2,
                "xact_commit": 6,
                "xact_rollback": 0,
                "blks_hit": 100,
                "blks_read": 0,
                "database_bytes": 8_000_000,
            },
        },
    )
    assert state_ok == "RECONCILED"
    assert reconciled["measured"]["process"]["total_cpu_ms"] == 20.0
    assert reconciled["measured"]["database"]["status"] == "MEASURED"
    assert reconciled["reconciliation"]["overspent"] is False
    assert reconciled["reconciliation"]["cpu_delta_ms"] == -2980.0

    # Overspend via step budget, total CPU, or worker-only CPU.
    _, overspent_steps = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=1000,
        terminal_status="COMPLETE",
        cancel_requested=False,
        valid_witness=True,
        consumed_steps=5,
        measurements={"worker_cpu_ms": 1200.0, "child_cpu_ms": None, "wall_ms": 1500.0},
    )
    assert overspent_steps["reconciliation"]["overspent"] is True
    assert set(overspent_steps["reconciliation"]["overspend_reasons"]) == {
        "step_budget_exceeded",
        "worker_cpu_reservation_exceeded",
    }

    _, overspent_total = reconcile_run_accounting(
        execution_backend="memory",
        step_budget=4,
        reserved_cpu_ms=500,
        terminal_status="COMPLETE",
        cancel_requested=False,
        valid_witness=True,
        consumed_steps=4,
        measurements={"worker_cpu_ms": 300.0, "child_cpu_ms": 300.0, "wall_ms": 700.0},
    )
    assert overspent_total["reconciliation"]["overspent"] is True
    assert overspent_total["reconciliation"]["overspend_reasons"] == ["cpu_reservation_exceeded"]


@pytest.fixture
def accounting_lab(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE model_reservation, model_budget, tool_audit, agent_plan, "
            "tool_capability, oracle_proposal, run_transition, experiment_run, "
            "project_quota CASCADE"
        )
        conn.execute(
            """
            INSERT INTO project_quota(project_id, max_open, max_reserved_cpu_ms)
            VALUES ('local', 20, 180000)
            ON CONFLICT (project_id) DO UPDATE
            SET max_open = 20, max_reserved_cpu_ms = 180000
            """
        )
    proposed = store.propose("accounting-test")
    approved = store.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "accounting-secret")
    return store, UUID(approved["id"])


def test_transactional_cpu_reservation_quota_and_concurrency(accounting_lab):
    store, oracle_id = accounting_lab
    with store.connect() as conn:
        conn.execute(
            "UPDATE project_quota SET max_open = 10, max_reserved_cpu_ms = 2500 "
            "WHERE project_id = 'local'"
        )

    def try_admit(idx: int):
        try:
            return store.admit(
                client_key=f"cpu-race-{idx}",
                variant="locked",
                schedule="serial",
                approved_oracle_id=oracle_id,
                reserved_cpu_ms=1000,
            )
        except ValueError as err:
            return str(err)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(try_admit, range(6)))

    admitted = [item for item in results if isinstance(item, dict)]
    rejected = [item for item in results if isinstance(item, str)]
    assert len(admitted) == 2
    assert len(rejected) == 4
    assert all("CPU reservation quota exhausted" in msg for msg in rejected)

    with TestClient(create_app()) as client:
        headers = {"X-Operator-Token": "accounting-secret"}
        preview = client.post(
            "/api/runs/preview",
            json={
                "variant": "locked",
                "schedule": "serial",
                "approved_oracle_id": str(oracle_id),
                "reserved_cpu_ms": 1000,
            },
            headers=headers,
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["preflight_ok"] is False
        assert "Active CPU reservation budget is full" in body["blockers"]
        assert body["budget"]["open_reserved_cpu_ms"] == 2000
        assert body["budget"]["max_reserved_cpu_ms"] == 2500
        assert body["budget"]["cpu_budget_available"] is False

        api_reject = client.post(
            "/api/runs",
            json={
                "client_key": "api-cpu-over-quota",
                "variant": "locked",
                "schedule": "serial",
                "approved_oracle_id": str(oracle_id),
                "reserved_cpu_ms": 1000,
            },
            headers=headers,
        )
        assert api_reject.status_code == 429


def test_real_worker_measures_memory_and_native_db_resources(accounting_lab, monkeypatch):
    target_admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not target_admin:
        pytest.skip("Separate native target PostgreSQL cluster is required")
    store, oracle_id = accounting_lab
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", target_admin)
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", target_admin)
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "subprocess")

    mem_run = store.admit(
        client_key="worker-mem-run",
        variant="locked",
        schedule="serial",
        approved_oracle_id=oracle_id,
    )
    assert mem_run["accounting_state"] == "RESERVED"
    assert mem_run["reserved_cpu_ms"] == DEFAULT_RESERVED_CPU_MS
    assert asyncio.run(process_one(store, "acct-worker-1")) is True

    mem_done = store.get(UUID(mem_run["run_id"]))
    assert mem_done["status"] == "COMPLETE"
    mem_acct = mem_done["resource_accounting"]
    assert mem_acct["measured"]["process"]["worker_cpu_ms"] >= 0
    assert mem_acct["measured"]["application"]["consumed_steps"] == 4
    assert mem_acct["measured"]["application"]["wall_ms"] > 0
    assert mem_acct["measured"]["database"]["status"] == "NOT_APPLICABLE"
    assert mem_acct["measured"]["database"]["active_ms"] is None
    assert mem_acct["estimated"]["provider_cost_usd"] is None
    if os.name == "posix":
        assert mem_done["accounting_state"] == "RECONCILED"
        assert mem_acct["measured"]["process"]["child_cpu_ms"] >= 0
        assert mem_acct["reconciliation"]["overspent"] is False
    else:
        assert mem_done["accounting_state"] == "PARTIAL"
        assert mem_acct["measured"]["process"]["child_cpu_ms"] is None

    native_run = store.admit(
        client_key="worker-native-run",
        variant="racy",
        schedule="overlap",
        approved_oracle_id=oracle_id,
        execution_backend="native-postgresql",
    )
    assert asyncio.run(process_one(store, "acct-worker-2")) is True
    native_done = store.get(UUID(native_run["run_id"]))
    assert native_done["status"] == "COMPLETE"
    assert native_done["verdict"] == "FAIL"
    native_acct = native_done["resource_accounting"]
    db_layer = native_acct["measured"]["database"]
    assert db_layer["status"] == "MEASURED"
    assert db_layer["active_ms"] >= 0
    assert db_layer["xact_commit"] >= 1
    assert db_layer["blks_hit"] >= 1
    assert db_layer["database_bytes"] > 0
    assert store.target_instances(UUID(native_run["run_id"]))[0]["status"] == "CLEANED"

    # Confirm the disposable target database is actually dropped after sampling.
    with psycopg.connect(target_admin, autocommit=True) as conn:
        leftover = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'cb_target_%'"
        ).fetchall()
        assert leftover == []


def test_overspend_cancel_latency_and_uncertainty_reporting(accounting_lab):
    store, oracle_id = accounting_lab

    # 1. Pending cancellation releases reservation and records ~0s latency.
    pending = store.admit(
        client_key="cancel-pending-acct",
        variant="locked",
        schedule="serial",
        approved_oracle_id=oracle_id,
        reserved_cpu_ms=1500,
    )
    pending_id = UUID(pending["run_id"])
    cancelled = store.cancel(pending_id, pending["version"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["accounting_state"] == "RELEASED"
    assert cancelled["resource_accounting"]["reconciliation"]["overspent"] is False

    # 2. In-flight cancellation -> UNKNOWN -> UNCERTAIN + latency sample.
    inflight = store.admit(
        client_key="cancel-inflight-acct",
        variant="racy",
        schedule="overlap",
        approved_oracle_id=oracle_id,
        reserved_cpu_ms=500,
    )
    inflight_id = UUID(inflight["run_id"])
    claim = store.claim("cancel-latency-worker")
    assert claim and claim["run_id"] == inflight["run_id"]
    cancelling = store.cancel(inflight_id, claim["version"])
    assert cancelling["status"] == "CANCELLING"
    with store.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '35 seconds' "
            "WHERE id = %s",
            (inflight_id,),
        )
        conn.execute(
            "UPDATE run_transition SET created_at = now() - interval '35 seconds' "
            "WHERE run_id = %s AND note = 'operator requested cancellation'",
            (inflight_id,),
        )
    stuck_snap = store.operations()
    assert stuck_snap["cancellation"]["in_flight"] == 1
    assert stuck_snap["cancellation"]["oldest_in_flight_seconds"] >= 30
    stuck_alerts = [a["code"] for a in diagnose_operations(stuck_snap)["alerts"]]
    assert "CANCELLATION_PENDING_TOO_LONG" in stuck_alerts

    assert store.reconcile_expired_cancelled() == 1
    expired = store.get(inflight_id)
    assert expired["status"] == "UNKNOWN"
    assert expired["accounting_state"] == "UNCERTAIN"
    assert expired["resource_accounting"]["reconciliation"]["overspent"] is None

    # 3. CPU overspend run -> overspent_count = 1 and alert fired.
    overspent_run = store.admit(
        client_key="cpu-overspend-run",
        variant="locked",
        schedule="serial",
        approved_oracle_id=oracle_id,
        reserved_cpu_ms=500,
    )
    os_id = UUID(overspent_run["run_id"])
    os_claim = store.claim("overspend-worker")
    assert os_claim and os_claim["run_id"] == overspent_run["run_id"]
    witness = asyncio.run(execute("locked", "serial"))
    assert store.finish(
        os_id,
        "overspend-worker",
        os_claim["lease_generation"],
        witness,
        measurements={"worker_cpu_ms": 620.0, "child_cpu_ms": 150.0, "wall_ms": 900.0},
    )
    os_done = store.get(os_id)
    assert os_done["accounting_state"] == "RECONCILED"
    assert os_done["resource_accounting"]["reconciliation"]["overspent"] is True
    assert os_done["resource_accounting"]["reconciliation"]["cpu_delta_ms"] == 270.0

    # 4. Retry-exhausted claim -> UNCERTAIN.
    exhausted = store.admit(
        client_key="retry-exhausted-acct",
        variant="locked",
        schedule="serial",
        approved_oracle_id=oracle_id,
    )
    ex_id = UUID(exhausted["run_id"])
    assert store.claim("retry-worker")
    with store.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_generation = 3, "
            "lease_until = now() - interval '1 second' WHERE id = %s",
            (ex_id,),
        )
    assert store.claim("sweep-worker") is None
    ex_done = store.get(ex_id)
    assert ex_done["status"] == "UNKNOWN"
    assert ex_done["accounting_state"] == "UNCERTAIN"

    snap = store.operations()
    assert snap["resources"]["open_runs"] == 0
    assert snap["resources"]["runs_7d"]["overspent_count"] == 1
    assert snap["resources"]["runs_7d"]["uncertain_count"] == 2
    assert snap["resources"]["runs_7d"]["released_count"] == 1
    assert snap["resources"]["runs_7d"]["overspend_rate"] == 1.0
    assert snap["resources"]["runs_7d"]["measured_worker_cpu_ms_total"] == pytest.approx(620.0)
    assert snap["resources"]["runs_7d"]["measured_db_active_ms_total"] is None
    assert snap["cancellation"]["sample_count"] == 2
    assert snap["cancellation"]["in_flight"] == 0
    assert snap["cancellation"]["p95_seconds"] is not None
    assert snap["cancellation"]["max_seconds"] >= 34.0

    alert_codes = {a["code"] for a in diagnose_operations(snap)["alerts"]}
    assert {"RESOURCE_OVERSPEND_DETECTED", "UNCERTAIN_RESOURCE_ACCOUNTING"} <= alert_codes
    prom = operational_metrics(snap)
    assert "clashbolt_runs_overspent_7d 1" in prom
    assert "clashbolt_runs_accounting_uncertain_7d 2" in prom
    assert "clashbolt_measured_worker_cpu_ms_7d 620.000" in prom
    assert "clashbolt_measured_db_active_ms_7d" not in prom
    assert "clashbolt_cancel_samples_7d 2" in prom
    assert "clashbolt_cancel_latency_p95_seconds " in prom


def test_model_budget_reconciliation_and_project_isolation(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    accounts = AccountStore(dsn)
    accounts.migrate()
    with accounts.connect() as conn:
        conn.execute(
            "TRUNCATE account_login_throttle, local_account, model_reservation, "
            "model_budget, oracle_proposal, experiment_run CASCADE"
        )
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open, max_reserved_cpu_ms) "
            "VALUES ('local', 20, 180000) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = 20, max_reserved_cpu_ms = 180000"
        )
    accounts.create_account("acct.op", "operator", "project-iso-password-1234")
    accounts.create_account("acct.rev", "reviewer", "project-iso-password-1234")
    accounts.create_account("acct.view", "viewer", "project-iso-password-1234")
    p2 = f"iso-{uuid4().hex[:8]}"
    p2_schema = accounts.create_project(p2)
    for user, role in (("acct.op", "operator"), ("acct.rev", "reviewer"), ("acct.view", "viewer")):
        accounts.grant_project_role(user, role, "local")
        accounts.grant_project_role(user, role, p2)

    local_policy = Policy(dsn)
    assert local_policy.local_model_budget()["reconciliation"]["accounting_status"] == "NO_USAGE"
    res1 = local_policy.reserve_model_call()
    assert local_policy.local_model_budget()["reconciliation"]["accounting_status"] == "RESERVED"
    with local_policy.connect() as conn:
        conn.execute(
            "UPDATE model_reservation SET deadline_at = now() - interval '1 second' WHERE id = %s",
            (res1,),
        )
    assert local_policy.reconcile_stalled_model_calls() == 1
    unknown_budget = local_policy.local_model_budget()
    assert unknown_budget["reconciliation"]["accounting_status"] == "UNCERTAIN"
    assert unknown_budget["reconciliation"]["measured_tokens"] is None
    assert unknown_budget["reconciliation"]["tokens_uncertain_reserved"] == 256

    res2 = local_policy.reserve_model_call()
    assert local_policy.settle_model_call(res2, 64)
    local_prop = local_policy.propose("iso-local")
    local_app = local_policy.approve(UUID(local_prop["id"]), ORACLE_HASH, "acct.rev")
    local_policy.admit(
        client_key="iso-local-run",
        variant="locked",
        schedule="serial",
        approved_oracle_id=UUID(local_app["id"]),
        reserved_cpu_ms=4000,
    )

    p2_policy = Policy(dsn, schema=p2_schema, project_id=p2)
    assert p2_policy.operations()["resources"]["open_reserved_cpu_ms"] == 0
    assert p2_policy.operations()["resources"]["model_budget"]["calls_reserved"] == 0

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "iso-session-secret-at-least-32-chars")
    with TestClient(create_app()) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "acct.op", "password": "project-iso-password-1234"},
        ).status_code == 200
        local_ops = client.get("/api/operations").json()
        assert local_ops["resources"]["project_id"] == "local"
        assert local_ops["resources"]["open_reserved_cpu_ms"] == 4000
        assert local_ops["resources"]["model_budget"]["calls_unknown"] == 1
        assert client.post("/api/auth/select-project", json={"project_id": p2}).status_code == 200
        p2_ops = client.get("/api/operations").json()
        assert p2_ops["resources"]["project_id"] == p2
        assert p2_ops["resources"]["open_reserved_cpu_ms"] == 0
        assert p2_ops["resources"]["model_budget"]["calls_reserved"] == 0

        assert client.post(
            "/api/auth/login",
            json={"username": "acct.view", "password": "project-iso-password-1234"},
        ).status_code == 200
        assert client.get("/api/operations").status_code == 403
        assert client.get("/api/metrics").status_code == 403


def test_account_migrate_upgrades_existing_project_schemas():
    from psycopg import sql as pg_sql

    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    accounts = AccountStore(dsn)
    accounts.migrate()
    project_id = f"upgr-{uuid4().hex[:8]}"
    schema = accounts.create_project(project_id)
    with accounts.connect() as conn:
        conn.execute(
            pg_sql.SQL("ALTER TABLE {}.experiment_run DROP COLUMN reserved_cpu_ms").format(
                pg_sql.Identifier(schema)
            )
        )
        conn.execute(
            pg_sql.SQL(
                "UPDATE {}.schema_revision SET version = 8 WHERE component = 'control'"
            ).format(pg_sql.Identifier(schema))
        )
    accounts.migrate()
    with accounts.connect() as conn:
        present = conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'experiment_run'
              AND column_name = 'reserved_cpu_ms'
            """,
            (schema,),
        ).fetchone()
        assert present is not None
        rev = conn.execute(
            pg_sql.SQL("SELECT version FROM {}.schema_revision WHERE component = 'control'").format(
                pg_sql.Identifier(schema)
            )
        ).fetchone()
        from clashbolt.store import BASE_SCHEMA_VERSION

        assert rev["version"] == BASE_SCHEMA_VERSION
