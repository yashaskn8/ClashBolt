"""B3 target reset latency and B4 controlled-vs-ungated gate overhead tests."""

import asyncio
import math
import os
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH
from clashbolt.metrics import operational_metrics
from clashbolt.policy import Policy
from clashbolt.ungated import measure_gate_overhead
from clashbolt.worker import process_one


@pytest.fixture
def native_lab(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not dsn or not admin:
        pytest.skip("Separate disposable PostgreSQL clusters are required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE target_instance, tool_audit, agent_plan, tool_capability, "
            "oracle_proposal, run_transition, experiment_run CASCADE"
        )
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open, max_reserved_cpu_ms) "
            "VALUES ('local', 20, 180000) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = 20, max_reserved_cpu_ms = 180000"
        )
    proposed = store.propose("reset-overhead-test")
    approved = store.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", admin)
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "subprocess")
    return store, admin, UUID(approved["id"])


def test_mark_target_rejects_invalid_latencies(native_lab):
    store, _, _ = native_lab
    for bad in (True, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="Target latency"):
            store.mark_target(uuid4(), "ACTIVE", provision_ms=bad)


def test_native_run_records_target_reset_latencies_and_metrics(native_lab):
    store, _, oracle_id = native_lab
    run = store.admit(
        client_key="reset-latency-run",
        variant="racy",
        schedule="overlap",
        approved_oracle_id=oracle_id,
        execution_backend="native-postgresql",
    )
    run_id = UUID(run["run_id"])
    assert asyncio.run(process_one(store, "reset-worker")) is True

    instances = store.target_instances(run_id)
    assert len(instances) == 1
    assert instances[0]["status"] == "CLEANED"
    assert instances[0]["provision_ms"] > 0
    assert instances[0]["teardown_ms"] > 0

    finished = store.get(run_id)
    db_layer = finished["resource_accounting"]["measured"]["database"]
    assert db_layer["provision_ms"] == instances[0]["provision_ms"]
    assert db_layer["teardown_ms"] == instances[0]["teardown_ms"]

    ops = store.operations()
    reset = ops["target_reset"]
    assert reset["sample_count"] == 1
    assert reset["unknown_count"] == 0
    assert reset["provision_p95_ms"] > 0
    assert reset["teardown_p95_ms"] > 0
    assert reset["reset_p95_ms"] > 0

    prom = operational_metrics(ops)
    assert "clashbolt_target_reset_samples_7d 1" in prom
    assert "clashbolt_target_reset_unknown_7d 0" in prom
    assert "clashbolt_target_reset_p95_ms " in prom


def test_measure_gate_overhead_compares_controlled_and_ungated_native(native_lab):
    store, admin, _ = native_lab
    with pytest.raises(ValueError, match="Invalid"):
        measure_gate_overhead(admin, store.dsn, variant="racy", schedule="bad", trials=2)
    with pytest.raises(ValueError, match="Invalid"):
        measure_gate_overhead(admin, store.dsn, variant="racy", schedule="overlap", trials=0)

    report = measure_gate_overhead(
        admin, store.dsn, variant="racy", schedule="overlap", trials=2
    )
    assert report["scope"] == "SHIPPED_SYNTHETIC_FIXTURE_ONLY"
    assert report["general_target_supported"] is False
    assert report["trials"] == 2
    assert report["controlled_fail_count"] == 2
    assert report["controlled_median_ms"] > 0
    assert report["ungated_median_ms"] > 0
    assert report["target_provision_median_ms"] > 0
    assert isinstance(report["gate_overhead_median_ms"], float)
