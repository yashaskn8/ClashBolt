"""Native PostgreSQL integration tests. Set CLASHBOLT_TEST_DATABASE_URL to run."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy
from clashbolt.worker import process_one


@pytest.fixture
def ledger():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL for native PostgreSQL integration")
    result = Policy(dsn)
    result.migrate()
    # Only this dedicated test database is touched; each test begins isolated.
    with result.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposed = result.propose("fixture-proposer")
    approved = result.approve(UUID(proposed["id"]), ORACLE_HASH, "fixture-reviewer")
    result.approval_id = UUID(approved["id"])
    return result


def admit(ledger, **kwargs):
    return ledger.admit(approved_oracle_id=ledger.approval_id, **kwargs)


def test_admission_is_idempotent_and_rejects_conflicting_payloads(ledger):
    key = str(uuid4())
    first = admit(ledger, client_key=key, variant="racy", schedule="overlap")
    second = admit(ledger, client_key=key, variant="racy", schedule="overlap")
    assert first["run_id"] == second["run_id"]
    assert first["status"] == "PENDING"
    with pytest.raises(ValueError, match="Idempotency"):
        admit(ledger, client_key=key, variant="locked", schedule="overlap")
    assert [e["status"] for e in ledger.transitions(UUID(first["run_id"]))] == ["PENDING"]


def test_worker_publishes_one_result_and_transitions(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    assert asyncio.run(process_one(ledger, "integration-worker"))
    result = ledger.get(UUID(run["run_id"]))
    assert result["status"] == "COMPLETE"
    assert result["verdict"] == "FAIL"
    assert result["consumed_steps"] == 4
    assert result["step_budget"] == 4
    assert [e["status"] for e in ledger.transitions(UUID(run["run_id"]))] == [
        "PENDING", "RUNNING", "COMPLETE"
    ]


def test_expired_generation_is_fenced(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
    first = ledger.claim("worker-one")
    assert first is not None
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' WHERE id = %s",
            (UUID(first["run_id"]),),
        )
    second = ledger.claim("worker-two")
    assert second is not None
    assert second["run_id"] == first["run_id"]
    witness = asyncio.run(execute("locked", "serial"))
    assert not ledger.finish(UUID(run["run_id"]), "worker-one", first["lease_generation"], witness)
    assert ledger.finish(UUID(run["run_id"]), "worker-two", second["lease_generation"], witness)
    assert ledger.get(UUID(run["run_id"]))["verdict"] == "PASS"


def test_cancel_pending_and_inflight_unknown(ledger):
    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="serial")
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLED"

    active = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("cancel-worker")
    assert claim and claim["run_id"] == active["run_id"]
    assert ledger.cancel(UUID(active["run_id"]))["status"] == "CANCELLING"
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(active["run_id"]), "cancel-worker", claim["lease_generation"], witness
    )
    result = ledger.get(UUID(active["run_id"]))
    assert result["status"] == "UNKNOWN"
    assert result["verdict"] == "UNKNOWN"


def test_quota_uses_single_serialized_row(ledger):
    key = str(uuid4())
    with ledger.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) VALUES ('local', 1) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = EXCLUDED.max_open"
        )
    try:
        # Other tests may leave active runs: use a unique local workspace test DB per job.
        with ledger.connect() as conn:
            conn.execute("UPDATE experiment_run SET status = 'CANCELLED' WHERE status = 'PENDING'")
        first = admit(ledger, client_key=key, variant="racy", schedule="overlap")
        with pytest.raises(ValueError, match="quota"):
            admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
        ledger.cancel(UUID(first["run_id"]))
    finally:
        with ledger.connect() as conn:
            conn.execute("UPDATE project_quota SET max_open = 20 WHERE project_id = 'local'")


def test_forged_witness_is_never_accepted_as_failure(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
    claim = ledger.claim("checker-test")
    assert claim and claim["run_id"] == run["run_id"]
    forged = asyncio.run(execute("locked", "serial"))
    forged["verdict"] = "FAIL"
    assert ledger.finish(UUID(run["run_id"]), "checker-test", claim["lease_generation"], forged)
    result = ledger.get(UUID(run["run_id"]))
    assert result["status"] == "UNKNOWN"
    assert result["verdict"] == "UNKNOWN"


def test_recent_runs_are_resumable(ledger):
    first = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="serial")
    second = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="overlap")
    listed = ledger.recent(10)
    ids = {item["run_id"] for item in listed}
    assert {first["run_id"], second["run_id"]}.issubset(ids)
    assert all("lease_owner" not in item for item in listed)
    with pytest.raises(ValueError, match="limit"):
        ledger.recent(101)


def test_concurrent_duplicate_admissions_use_one_run(ledger):
    key = str(uuid4())

    def admit_once(_):
        return admit(ledger, client_key=key, variant="racy", schedule="overlap")

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(admit_once, range(8)))
    assert len({item["run_id"] for item in accepted}) == 1
    assert len(ledger.recent()) == 1


def test_concurrent_quota_admission_fails_closed(ledger):
    with ledger.connect() as conn:
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) VALUES ('local', 1) "
            "ON CONFLICT (project_id) DO UPDATE SET max_open = EXCLUDED.max_open"
        )

    def attempt(_):
        try:
            return admit(ledger, 
                client_key=str(uuid4()), variant="locked", schedule="serial"
            )["run_id"]
        except ValueError as error:
            assert "quota" in str(error)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert len([item for item in outcomes if item is not None]) == 1


def test_expired_inflight_cancellation_becomes_unknown(ledger):
    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claimed = ledger.claim("stall-worker")
    assert claimed and claimed["run_id"] == queued["run_id"]
    assert ledger.cancel(UUID(queued["run_id"]))["status"] == "CANCELLING"
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE experiment_run SET lease_until = now() - interval '1 second' "
            "WHERE id = %s", (UUID(queued["run_id"]),),
        )
    assert ledger.reconcile_expired_cancelled() == 1
    current = ledger.get(UUID(queued["run_id"]))
    assert current["status"] == "UNKNOWN"
    assert current["verdict"] == "UNKNOWN"



def test_admission_rejects_unapproved_rule(ledger):
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(client_key=str(uuid4()), variant="racy", schedule="overlap")
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(
            client_key=str(uuid4()), variant="racy", schedule="overlap",
            approved_oracle_id=uuid4(),
        )


def test_oracle_revocation_withdraws_existing_verdict(ledger):
    run = ledger.admit(
        approved_oracle_id=ledger.approval_id,
        client_key=str(uuid4()), variant="racy", schedule="overlap",
    )
    claim = ledger.claim("revocation-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(run["run_id"]), "revocation-worker", claim["lease_generation"], witness
    )
    assert ledger.get(UUID(run["run_id"]))["verdict"] == "FAIL"
    assert ledger.revoke_rule(ledger.approval_id)
    after = ledger.get(UUID(run["run_id"]))
    assert after["verdict"] == "UNKNOWN"
    assert after["status"] == "UNKNOWN"
    assert ledger.transitions(UUID(run["run_id"]))[-1]["note"].startswith(
        "oracle approval revoked"
    )
    with pytest.raises(ValueError, match="approved"):
        ledger.admit(
            approved_oracle_id=ledger.approval_id,
            client_key=str(uuid4()), variant="racy", schedule="overlap",
        )


def test_step_budget_excess_never_certifies_oversized_trace(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("budget-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    witness["observed_steps"].extend(witness["observed_steps"])
    assert ledger.finish(
        UUID(run["run_id"]), "budget-worker", claim["lease_generation"], witness
    )
    saved = ledger.get(UUID(run["run_id"]))
    assert saved["status"] == "UNKNOWN"
    assert saved["verdict"] == "UNKNOWN"
    assert saved["consumed_steps"] == 8


def test_revoked_witness_replay_cannot_regain_certification(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("replay-worker")
    assert claim and claim["run_id"] == queued["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(queued["run_id"]), "replay-worker", claim["lease_generation"], witness
    )
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "test-operator-secret")
    headers = {"X-Operator-Token": "test-operator-secret"}
    with TestClient(create_app()) as client:
        accepted = client.post(f"/api/runs/{queued['run_id']}/replay", headers=headers)
        assert accepted.status_code == 200
        assert accepted.json()["replay_status"] == "REPRODUCED"
        assert ledger.revoke_rule(ledger.approval_id)
        withdrawn = client.post(f"/api/runs/{queued['run_id']}/replay", headers=headers)
        assert withdrawn.status_code == 200
        assert withdrawn.json()["replay_status"] == "UNCONFIRMED"
        assert withdrawn.json()["adjudicated_verdict"] == "UNKNOWN"


def test_durable_mutations_require_operator_token(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "test-operator-secret")
    payload = {
        "client_key": str(uuid4()),
        "variant": "racy",
        "schedule": "overlap",
        "approved_oracle_id": str(ledger.approval_id),
    }
    with TestClient(create_app()) as client:
        assert client.post("/api/runs", json=payload).status_code == 403
        assert client.post(
            "/api/runs", json=payload, headers={"X-Operator-Token": "incorrect"}
        ).status_code == 403
        headers = {"X-Operator-Token": "test-operator-secret"}
        response = client.post("/api/runs", json=payload, headers=headers)
        assert response.status_code == 201
        run_id = response.json()["run_id"]
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 403
        assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).status_code == 200


def test_revocation_race_never_leaves_certified_verdict(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("race-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        finishing = pool.submit(
            ledger.finish, UUID(run["run_id"]), "race-worker",
            claim["lease_generation"], witness,
        )
        revoking = pool.submit(ledger.revoke_rule, ledger.approval_id)
        assert finishing.result(timeout=15)
        assert revoking.result(timeout=15)

    current = ledger.get(UUID(run["run_id"]))
    assert current["status"] == "UNKNOWN"
    assert current["verdict"] == "UNKNOWN"
    assert current["witness"] is not None


def test_reduction_report_is_bound_to_current_verdict(ledger):
    from clashbolt.reducer import reduce_witness

    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("reducer-worker")
    assert claim and claim["run_id"] == run["run_id"]
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(run["run_id"]), "reducer-worker", claim["lease_generation"], witness
    )
    after = ledger.get(UUID(run["run_id"]))
    report = reduce_witness(witness)
    run_id = UUID(run["run_id"])
    assert ledger.record_reduction(run_id, after["version"], report)
    assert ledger.get_reduction(run_id)["status"] == "MINIMAL"
    assert ledger.revoke_rule(ledger.approval_id)
    assert ledger.get_reduction(run_id) is None
    assert ledger.record_reduction(run_id, after["version"], report) is None



def test_transition_cursor_paginates_history_without_loss(ledger):
    run = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="serial")
    run_id = UUID(run["run_id"])
    with ledger.connect() as conn:
        conn.execute(
            """
            INSERT INTO run_transition(run_id, version, status, note)
            SELECT %s, n, 'PENDING', 'synthetic history page'
            FROM generate_series(2, 125) AS n
            """,
            (run_id,),
        )
    first = ledger.transition_page(run_id, limit=100)
    assert first["has_more"]
    assert first["next_after"] == 100
    assert len(first["events"]) == 100
    second = ledger.transition_page(run_id, after=first["next_after"])
    assert not second["has_more"]
    assert second["next_after"] == 125
    assert len(second["events"]) == 25
    assert [row["version"] for row in first["events"] + second["events"]] == list(
        range(1, 126)
    )
    with pytest.raises(ValueError):
        ledger.transition_page(run_id, after=-1)
    with pytest.raises(ValueError):
        ledger.transition_page(run_id, limit=101)


def test_draft_version_cas_rejects_stale_overwrites(ledger):
    first = ledger.save_draft({
        "variant": "racy", "schedule": "serial", "execution_backend": "memory",
    }, expected_version=0)
    assert first["version"] == 1
    assert ledger.get_draft()["draft"]["schedule"] == "serial"
    with pytest.raises(ValueError, match="Stale"):
        ledger.save_draft({
            "variant": "locked", "schedule": "overlap",
            "execution_backend": "native-postgresql",
        }, expected_version=0)
    updated = ledger.save_draft({
        "variant": "locked", "schedule": "overlap",
        "execution_backend": "native-postgresql",
    }, expected_version=first["version"])
    assert updated["version"] == 2
    assert ledger.get_draft()["draft"]["variant"] == "locked"
    assert "approved_oracle_id" not in updated["draft"]


def test_draft_api_requires_operator_credential(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "draft-secret")
    payload = {
        "expected_version": 0, "variant": "racy",
        "schedule": "overlap", "execution_backend": "memory",
    }
    with TestClient(create_app()) as client:
        assert client.get("/api/experiment-draft").json()["saved"] is None
        assert client.post("/api/experiment-draft", json=payload).status_code == 403
        headers = {"X-Operator-Token": "draft-secret"}
        saved = client.post("/api/experiment-draft", json=payload, headers=headers)
        assert saved.status_code == 200
        assert client.post(
            "/api/experiment-draft", json=payload, headers=headers
        ).status_code == 409



def test_migrations_are_atomic_and_serialize_competing_starts(ledger):
    from clashbolt.policy import POLICY_SCHEMA_VERSION
    from clashbolt.store import BASE_SCHEMA_VERSION

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: ledger.migrate(), range(4))) == [
            None, None, None, None
        ]
    with ledger.connect() as conn:
        rows = conn.execute(
            "SELECT component, version FROM schema_revision ORDER BY component"
        ).fetchall()
        revisions = {row["component"]: row["version"] for row in rows}
    assert revisions["control"] == BASE_SCHEMA_VERSION
    assert revisions["policy"] == POLICY_SCHEMA_VERSION
    # Optional account or future component revisions may coexist.


def test_migration_refuses_unknown_future_schema(ledger):
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE schema_revision SET version = 999 WHERE component = 'policy'"
        )
    try:
        with pytest.raises(ValueError, match="newer"):
            ledger.migrate()
    finally:
        from clashbolt.policy import POLICY_SCHEMA_VERSION

        with ledger.connect() as conn:
            conn.execute(
                "UPDATE schema_revision SET version = %s WHERE component = 'policy'",
                (POLICY_SCHEMA_VERSION,),
            )



def test_certified_run_can_export_bounded_bundle(ledger, monkeypatch):
    import asyncio
    import io
    import zipfile

    from clashbolt.api import create_app
    from clashbolt.experiment import execute
    from fastapi.testclient import TestClient

    queued = admit(ledger, client_key=str(uuid4()), variant="racy", schedule="overlap")
    claim = ledger.claim("zip-worker")
    assert claim
    witness = asyncio.run(execute("racy", "overlap"))
    assert ledger.finish(
        UUID(queued["run_id"]), "zip-worker", claim["lease_generation"], witness
    )
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    with TestClient(create_app()) as client:
        response = client.get(f"/api/runs/{queued['run_id']}/bundle")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert len(response.content) < 100_000
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "witness.json" in archive.namelist()
        assert ledger.revoke_rule(ledger.approval_id)
        assert client.get(
            f"/api/runs/{queued['run_id']}/bundle"
        ).status_code == 409


def test_readiness_fails_closed_on_incompatible_database_revision(ledger, monkeypatch):
    from clashbolt.api import create_app
    from clashbolt.policy import POLICY_SCHEMA_VERSION
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    with TestClient(create_app()) as client:
        assert client.get("/api/ready").status_code == 200
        try:
            with ledger.connect() as conn:
                conn.execute(
                    "UPDATE schema_revision SET version = 999 WHERE component = 'policy'"
                )
            failed = client.get("/api/ready")
            assert failed.status_code == 503
            assert "incompatible" in failed.json()["detail"]
        finally:
            with ledger.connect() as conn:
                conn.execute(
                    """
                    UPDATE schema_revision SET version = %s
                    WHERE component = 'policy'
                    """,
                    (POLICY_SCHEMA_VERSION,),
                )
        assert client.get("/api/ready").status_code == 200


def test_worker_heartbeat_and_operator_queue_snapshot(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    ledger.heartbeat("worker-recent")
    ledger.heartbeat("worker-old")
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE worker_heartbeat SET last_seen = now() - interval '1 minute' "
            "WHERE owner = 'worker-old'"
        )
    snapshot = ledger.operations()
    assert snapshot["responsive_workers"] >= 1
    worker = next(row for row in snapshot["workers"] if row["owner"] == "worker-old")
    assert not worker["responsive"]
    assert "verifications" in snapshot
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "monitor-secret")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    with TestClient(create_app()) as client:
        assert client.get("/api/operations").status_code == 403
        response = client.get(
            "/api/operations", headers={"X-Operator-Token": "monitor-secret"}
        )
        assert response.status_code == 200
        assert response.json()["responsive_workers"] >= 1



def test_metrics_endpoint_is_operator_only_and_redacts_worker_ids(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "metrics-secret")
    ledger.heartbeat("sensitive-worker-name")
    with TestClient(create_app()) as client:
        assert client.get("/api/metrics").status_code == 403
        response = client.get(
            "/api/metrics", headers={"X-Operator-Token": "metrics-secret"}
        )
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert 'clashbolt_runs{state="PENDING"}' in response.text
        assert "clashbolt_responsive_workers" in response.text
        assert "sensitive-worker-name" not in response.text



def test_readiness_can_require_fresh_separate_worker_heartbeat(ledger, monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", ledger.dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE worker_heartbeat SET last_seen = now() - interval '2 hours'"
        )
    with TestClient(create_app()) as client:
        assert client.get("/api/ready").status_code == 200
        assert client.get("/api/ready?require_worker=true").status_code == 503
        ledger.heartbeat("ready-worker")
        response = client.get("/api/ready?require_worker=true")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"



def test_120_crash_and_duplicate_injections_fence_every_terminal_result(ledger):
    """Deterministic 120-case local crash matrix; no real host failure is claimed."""
    witness = asyncio.run(execute("locked", "serial"))
    outcomes = {"COMPLETE": 0, "UNKNOWN": 0}
    for index in range(120):
        created = admit(
            ledger, client_key=str(uuid4()), variant="locked", schedule="serial"
        )
        run_id = UUID(created["run_id"])
        owner = f"crash-injection-{index}"
        first = ledger.claim(owner, lease_seconds=5)
        assert first and first["run_id"] == created["run_id"]
        generation = first["lease_generation"]

        if index % 3 == 0:
            # The original worker missed its lease, then resumed too late.
            with ledger.connect() as conn:
                conn.execute(
                    "UPDATE experiment_run SET lease_until = now() - interval "
                    "'1 second' WHERE id = %s",
                    (run_id,),
                )
            second = ledger.claim(f"{owner}-replacement", lease_seconds=5)
            assert second and second["run_id"] == created["run_id"]
            assert not ledger.finish(run_id, owner, generation, witness)
            assert ledger.finish(
                run_id, f"{owner}-replacement", second["lease_generation"], witness
            )
            expected = ["PENDING", "RUNNING", "RUNNING", "COMPLETE"]
        elif index % 3 == 1:
            # Cancellation after claim never pretends the target was rolled back.
            assert ledger.cancel(run_id)["status"] == "CANCELLING"
            with ledger.connect() as conn:
                conn.execute(
                    "UPDATE experiment_run SET lease_until = now() - interval "
                    "'1 second' WHERE id = %s",
                    (run_id,),
                )
            assert ledger.reconcile_expired_cancelled() == 1
            assert not ledger.finish(run_id, owner, generation, witness)
            expected = ["PENDING", "RUNNING", "CANCELLING", "UNKNOWN"]
        else:
            assert ledger.finish(run_id, owner, generation, witness)
            assert not ledger.finish(run_id, owner, generation, witness)
            expected = ["PENDING", "RUNNING", "COMPLETE"]

        final = ledger.get(run_id)
        outcomes[final["status"]] += 1
        assert [event["status"] for event in ledger.transitions(run_id)] == expected
        assert final["verdict"] == (
            "UNKNOWN" if final["status"] == "UNKNOWN" else "PASS"
        )
    assert outcomes == {"COMPLETE": 80, "UNKNOWN": 40}


@pytest.mark.parametrize("witness", [None, {"oracle": []}, {"oracle": None}, {"bad": float("nan")}])
def test_invalid_worker_witness_becomes_unknown_without_attachment(ledger, witness):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
    claim = ledger.claim("malformed-result-worker")
    run_id = UUID(run["run_id"])
    assert ledger.finish(run_id, "malformed-result-worker", claim["lease_generation"], witness)
    current = ledger.get(run_id)
    assert (current["status"], current["verdict"], current["witness"]) == (
        "UNKNOWN", "UNKNOWN", None,
    )
    assert ledger.transitions(run_id)[-1]["note"] == "invalid or mismatched witness"


def test_run_cannot_finalize_with_lease_expired_after_transaction_started(ledger, monkeypatch):
    run = admit(ledger, client_key=str(uuid4()), variant="locked", schedule="serial")
    claim = ledger.claim("deadline-worker")
    run_id = UUID(run["run_id"])
    witness = asyncio.run(execute("locked", "serial"))
    original = ledger.connect

    def transaction_before_expiry():
        transaction = original()
        now = transaction.execute("SELECT now() AS started").fetchone()["started"]
        with original() as conn:
            conn.execute(
                """
                UPDATE experiment_run
                SET lease_until = %s::timestamptz + interval '1 microsecond'
                WHERE id = %s
                """,
                (now, run_id),
            )
        return transaction

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "connect", transaction_before_expiry)
        assert not ledger.finish(run_id, "deadline-worker", claim["lease_generation"], witness)
    assert ledger.get(run_id)["status"] == "RUNNING"
