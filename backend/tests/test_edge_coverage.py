"""Boundary and error-path tests for API, policy routes, planner, reducer, and witness."""

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from clashbolt import api, backup, planner, policy, reducer, witness
from clashbolt.accounts import AccountStore
from clashbolt.experiment import ORACLE_HASH, execute
from fastapi.testclient import TestClient


def test_valid_session_cookie_edge_cases():
    secret = "test-secret-32-chars-minimum-12345"
    assert api._valid_session(None, secret) is False
    assert api._valid_session("cookie", None) is False
    assert api._valid_session("malformed", secret) is False
    assert api._valid_session("not-digits.nonce." + "a" * 64, secret) is False
    assert api._valid_session("9999999999." + "n" * 65 + "." + "a" * 64, secret) is False
    token = api._signed_session(secret)
    expiry, nonce, _ = token.split(".")
    assert api._valid_session(f"{expiry}.{nonce}." + "0" * 64, secret) is False
    assert api._valid_session("0.nonce." + "a" * 64, secret) is False


def test_planner_validation_and_cli(monkeypatch, capsys):
    with pytest.raises(ValueError):
        planner.choose_plan(strategy="invalid", variant="racy")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        planner.choose_plan(strategy="semantic", variant="racy", seed=-1)
    with pytest.raises(ValueError):
        asyncio.run(planner.compare_strategies(trials=0))

    monkeypatch.setattr(sys, "argv", ["clashbolt-planner", "--trials", "1"])
    planner.main()
    captured = json.loads(capsys.readouterr().out)
    assert captured["promotion_eligible"] is False
    assert captured["strategies"]["semantic"]["trials"] == 1


def test_witness_and_reducer_failure_paths(monkeypatch):
    valid = asyncio.run(execute("racy", "overlap"))
    non_json = {**valid, "extra": {1, 2}}
    assert witness.validate_contract(non_json) == "Witness cannot be serialized"

    @contextmanager
    def broken_target():
        raise OSError("target cluster unreachable")
        yield  # pragma: no cover

    native_witness = {**valid, "execution_backend": "native-postgresql"}
    native_witness["evidence_sha256"] = witness.evidence_digest(native_witness)
    res_native = asyncio.run(witness.verify(native_witness, target_factory=broken_target))
    assert res_native == {"status": "UNCONFIRMED", "reason": "Native target replay failed"}

    monkeypatch.setattr(witness, "service_enabled", lambda: True)
    monkeypatch.setattr(
        witness,
        "request_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("service down")),
    )
    res_svc = asyncio.run(witness.verify(valid))
    assert res_svc == {"status": "UNCONFIRMED", "reason": "Target service replay failed"}

    assert reducer._probe("locked", ["a.commit"], None) == "UNKNOWN"
    pass_witness = asyncio.run(execute("locked", "serial"))
    non_fail_report = reducer.reduce_witness(pass_witness)
    assert non_fail_report["status"] == "UNCONFIRMED"
    missing_native_url = reducer.reduce_witness(native_witness)
    assert missing_native_url == {
        "status": "UNCONFIRMED",
        "reason": "Native test cluster is required",
    }

    monkeypatch.setattr(reducer, "service_enabled", lambda: True)
    monkeypatch.setattr(
        reducer, "request_fixture", lambda *_args, **_kwargs: {"verdict": "BAD"}
    )
    with pytest.raises(ValueError, match="Invalid target service probe verdict"):
        reducer._trial("racy", "overlap", ["a.read"], None)


def test_backup_input_validation_helpers():
    assert backup._loopback("not-an-ip-address") is False
    with pytest.raises(ValueError):
        backup._database_args("host=")
    with pytest.raises(ValueError):
        backup._database_args("postgresql://postgres:secret@127.0.0.1:5432/postgres")
    with pytest.raises(ValueError):
        with open(__file__, "rb") as stream:
            backup._restore_verified_archive(stream, "dbname=postgres")


def test_api_and_policy_routes_error_edges(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set CLASHBOLT_TEST_DATABASE_URL to a disposable PostgreSQL DB")

    accounts = AccountStore(dsn)
    accounts.migrate()
    with accounts.connect() as conn:
        conn.execute(
            "TRUNCATE account_login_throttle, local_account, witness_export, witness_review, "
            "verification_target_instance, verification_job, target_instance, "
            "run_reduction, run_transition, experiment_run, tool_audit, agent_plan, "
            "tool_capability, oracle_proposal CASCADE"
        )
    accounts.create_account("edge.operator", "operator", "edge-local-password-111")
    accounts.create_account("edge.reviewer", "reviewer", "edge-local-password-111")

    store = policy.Policy(dsn)
    with pytest.raises(ValueError):
        store.propose("")
    with pytest.raises(ValueError):
        store.oracle_audit(0)
    prop = store.propose("edge.operator")
    prop_uuid = UUID(prop["id"])
    with pytest.raises(ValueError):
        store.approve(prop_uuid, ORACLE_HASH, "")
    with pytest.raises(ValueError):
        store.revoke_rule(prop_uuid, "")
    with pytest.raises(ValueError):
        store.issue(prop_uuid, "racy", "overlap", issuer_account_id="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.issue(prop_uuid, "bad", "overlap")
    with pytest.raises(ValueError):
        store.issue(prop_uuid, "racy", "overlap", max_calls=99)

    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_SESSION_SECRET", "test-session-key-32-chars-long-1234567")
    monkeypatch.delenv("CLASHBOLT_OPERATOR_TOKEN", raising=False)

    with TestClient(api.create_app()) as client:
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "edge.operator", "password": "edge-local-password-111"},
            ).status_code
            == 200
        )
        # In-memory scratch experiment routes are disabled in accounts mode.
        assert (
            client.post(
                "/api/experiments", json={"variant": "racy", "schedule": "overlap"}
            ).status_code
            == 404
        )
        assert client.get(f"/api/experiments/{uuid4()}").status_code == 404
        assert client.post(f"/api/experiments/{uuid4()}/replay").status_code == 404
        assert client.get("/api/runs?limit=999").status_code == 422

        missing_id = uuid4()
        assert client.get(f"/api/runs/{missing_id}/witness").status_code == 404
        assert client.post(f"/api/runs/{missing_id}/replay").status_code == 404
        assert client.post(f"/api/runs/{missing_id}/reduce").status_code == 409
        assert client.get(f"/api/runs/{missing_id}/reduction").status_code == 404
        assert (
            client.post(
                f"/api/runs/{missing_id}/verification-jobs", json={"kind": "REPLAY"}
            ).status_code
            == 409
        )
        assert client.get(f"/api/verification-jobs/{missing_id}").status_code == 404
        assert client.get(f"/api/agent/plans/{missing_id}").status_code == 404
        assert client.get(f"/api/agent/plans/{missing_id}/history").status_code == 404
        assert client.get(f"/api/agent/plans/{missing_id}/feedback").status_code == 404
        assert (
            client.post(
                f"/api/agent/plans/{missing_id}/feedback", json={"remaining_budget": 4}
            ).status_code
            == 409
        )
        assert client.post(f"/api/tool-capabilities/{missing_id}/revoke").status_code == 404
        assert client.get(f"/api/agent/frontier/{missing_id}").status_code == 409
        assert (
            client.post(
                f"/api/agent/frontier/{missing_id}/plans",
                json={"proposal_id": str(uuid4()), "variant": "racy"},
            ).status_code
            == 409
        )

        # Admit a run and verify replay-without-witness and reduce-noncomplete paths.
        created_prop = client.post("/api/oracle/proposals", json={}).json()
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "edge.reviewer", "password": "edge-local-password-111"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/oracle/proposals/{created_prop['id']}/approve",
                json={"expected_hash": ORACLE_HASH, "reviewer": "edge.reviewer"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "edge.operator", "password": "edge-local-password-111"},
            ).status_code
            == 200
        )
        run = client.post(
            "/api/runs",
            json={
                "client_key": str(uuid4()),
                "variant": "racy",
                "schedule": "overlap",
                "approved_oracle_id": created_prop["id"],
            },
        )
        assert run.status_code == 201
        run_id = run.json()["run_id"]
        assert client.post(f"/api/runs/{run_id}/replay").status_code == 409

        # Trigger local model exhaustion (429) cleanly via CLASHBOLT_LOCAL_MODEL.
        monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "mock-local")
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO model_budget(project_id, window_date, call_limit, calls_reserved) "
                "VALUES ('local', CURRENT_DATE, 1, 1) "
                "ON CONFLICT (project_id, window_date) DO UPDATE "
                "SET call_limit = 1, calls_reserved = 1"
            )
        exhausted = client.post(
            "/api/agent/plans/model",
            json={"proposal_id": created_prop["id"], "variant": "racy"},
        )
        assert exhausted.status_code == 429


def test_accounts_and_worker_cli_error_edges(monkeypatch):
    from clashbolt import accounts, worker

    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["clashbolt-accounts", "migrate"])
    with pytest.raises(SystemExit):
        accounts.main()

    monkeypatch.setattr(sys, "argv", ["clashbolt-worker"])
    with pytest.raises(SystemExit):
        asyncio.run(worker.main())

    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "postgresql://user:pw@127.0.0.1:55432/db")
    monkeypatch.setattr(sys, "argv", ["clashbolt-worker", "--interval", "0.01"])
    with pytest.raises(SystemExit):
        asyncio.run(worker.main())

    monkeypatch.setattr(worker, "service_enabled", lambda: True)
    monkeypatch.setattr(worker, "request_fixture", lambda *_args, **_kwargs: {"invalid": True})
    with pytest.raises(ValueError, match="Target service returned invalid evidence"):
        asyncio.run(worker.isolated_synthetic_run("racy", "overlap"))
