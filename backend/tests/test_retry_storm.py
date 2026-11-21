"""Bounded retry-storm regression on the trusted synthetic PostgreSQL ledger."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.store import MAX_CLAIM_GENERATIONS


@pytest.fixture
def ledger():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Dedicated disposable native PostgreSQL test database required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        assert conn.info.dbname == "clashbolt_test"
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposal = store.propose("retry-storm-fixture")
    approved = store.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    return store, UUID(approved["id"])


def _admit(store, approved, key, backend="memory"):
    return store.admit(
        client_key=key, variant="locked", schedule="serial",
        approved_oracle_id=approved, execution_backend=backend,
    )


@pytest.mark.parametrize("backend", ("memory", "native-postgresql"))
def test_missing_receipt_retry_budget_is_terminal_and_fenced(
    ledger, monkeypatch, backend,
):
    store, approved = ledger
    if backend == "native-postgresql":
        target = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
        if not target:
            pytest.skip("Separate disposable native target cluster required")
        monkeypatch.setenv("CLASHBOLT_TARGET_CLUSTER_URL", target)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) "
            "VALUES ('local', 1) ON CONFLICT (project_id) "
            "DO UPDATE SET max_open = EXCLUDED.max_open"
        )
    key = str(uuid4())
    submitted = _admit(store, approved, key, backend)
    run_id = UUID(submitted["run_id"])
    owners = []
    for number in range(MAX_CLAIM_GENERATIONS):
        owner = f"abandoned-{number}"
        claimed = store.claim(owner, lease_seconds=5)
        assert claimed and claimed["run_id"] == submitted["run_id"]
        assert claimed["lease_generation"] == number + 1
        owners.append((owner, claimed["lease_generation"]))
        with store.connect() as conn:
            conn.execute(
                "UPDATE experiment_run "
                "SET lease_until = now() - interval '1 second' WHERE id = %s",
                (run_id,),
            )

    # Another claim now atomically records UNKNOWN rather than provisioning
    # a fourth target or spending another attempt under a retry storm.
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda i: store.claim(f"storm-worker-{i}", lease_seconds=5),
            range(12),
        ))
    assert results == [None] * 12
    terminal = store.get(run_id)
    assert terminal["status"] == "UNKNOWN"
    assert terminal["verdict"] == "UNKNOWN"
    assert terminal["lease_generation"] == MAX_CLAIM_GENERATIONS
    assert terminal["witness"] is None
    history = store.transitions(run_id)
    assert [item["status"] for item in history] == [
        "PENDING", *(["RUNNING"] * MAX_CLAIM_GENERATIONS), "UNKNOWN",
    ]
    assert "retry budget exhausted" in history[-1]["note"]
    for owner, generation in owners:
        assert not store.finish(run_id, owner, generation, {})
    retry = _admit(store, approved, key, backend)
    assert retry["run_id"] == submitted["run_id"]
    assert retry["status"] == "UNKNOWN"
    assert len(store.transitions(run_id)) == MAX_CLAIM_GENERATIONS + 2
    # A terminal unknown releases durable open-run quota for new work.
    fresh = _admit(store, approved, str(uuid4()), backend)
    assert fresh["status"] == "PENDING"
    replacement = store.claim("fresh-owner")
    assert replacement and replacement["run_id"] == fresh["run_id"]


def test_concurrent_idempotency_retry_storm_uses_single_quota_slot(ledger):
    store, approved = ledger
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) "
            "VALUES ('local', 1) ON CONFLICT (project_id) "
            "DO UPDATE SET max_open = EXCLUDED.max_open"
        )
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = list(pool.map(
            lambda _: _admit(store, approved, key), range(40)
        ))
    assert len({item["run_id"] for item in accepted}) == 1
    run_id = UUID(accepted[0]["run_id"])
    assert [event["status"] for event in store.transitions(run_id)] == ["PENDING"]
    with pytest.raises(ValueError, match="quota"):
        _admit(store, approved, str(uuid4()))
    with pytest.raises(ValueError, match="Idempotency"):
        store.admit(
            client_key=key, variant="racy", schedule="serial",
            approved_oracle_id=approved,
        )
