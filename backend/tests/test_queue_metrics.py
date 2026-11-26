"""Prometheus queue timing observations from authoritative native PostgreSQL."""

import os
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH
from clashbolt.metrics import operational_metrics
from clashbolt.policy import Policy


def test_pending_age_and_first_claim_p95_are_measurable():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Native PostgreSQL test database required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposed = store.propose("timing-metrics")
    approved = store.approve(UUID(proposed["id"]), ORACLE_HASH, "reviewer")
    run = store.admit(
        client_key=str(uuid4()), approved_oracle_id=UUID(approved["id"]),
        variant="locked", schedule="serial",
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET created_at = now() - interval '10 minutes' "
            "WHERE id = %s",
            (UUID(run["run_id"]),),
        )
    before = store.operations()
    assert before["oldest_pending_seconds"] >= 590
    assert before["start_p95_seconds"] == 0

    claim = store.claim("timing-worker")
    assert claim and claim["run_id"] == run["run_id"]
    after = store.operations()
    assert after["oldest_pending_seconds"] == 0
    assert after["start_p95_seconds"] >= 590
    metrics = operational_metrics(after)
    assert "clashbolt_oldest_pending_seconds 0.000" in metrics
    assert "clashbolt_run_start_p95_seconds " in metrics
    assert "10 minutes" not in metrics

    additional = [
        store.admit(
            client_key=str(uuid4()), approved_oracle_id=UUID(approved["id"]),
            variant="locked", schedule="serial",
        )
        for _ in range(3)
    ]
    samples = [
        (UUID(run["run_id"]), 1, 20),
        (UUID(additional[0]["run_id"]), 2, 100),
        (UUID(additional[1]["run_id"]), 8, 10_000),
    ]
    with store.connect() as conn:
        for run_id, days_ago, first_start in samples:
            conn.execute(
                """
                UPDATE experiment_run SET created_at = now() - %s * interval '1 day'
                WHERE id = %s
                """,
                (days_ago, run_id),
            )
            conn.execute(
                """
                INSERT INTO run_transition (run_id, version, status, note, created_at)
                SELECT id, event.version, 'RUNNING', '', r.created_at + event.offset_seconds
                    * interval '1 second'
                FROM experiment_run r
                CROSS JOIN (VALUES (2, %s), (3, %s)) AS event(version, offset_seconds)
                WHERE id = %s
                ON CONFLICT (run_id, version) DO UPDATE SET created_at = EXCLUDED.created_at
                """,
                (first_start, first_start + 300, run_id),
            )

    # The recent first claims are 20 and 100 seconds. Retries, the old run
    # and the unclaimed fourth run contribute no samples.
    assert store.operations()["start_p95_seconds"] == pytest.approx(96.0)
