"""P3 reproducible, immutable synthetic bundle export evidence."""

import asyncio
import hashlib
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.api import create_app
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.witness_bundle import verify_bundle
from clashbolt.worker import process_one
from fastapi.testclient import TestClient


@pytest.fixture
def export_lab(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL is required")
    store = Policy(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute("TRUNCATE oracle_proposal, experiment_run CASCADE")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", dsn)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "immutable-export-test-secret")
    return store


def _certified_run(store):
    proposal = store.propose("export-author")
    oracle_id = UUID(proposal["id"])
    store.approve(oracle_id, ORACLE_HASH, "export-reviewer")
    admitted = store.admit(
        client_key=str(uuid4()), variant="racy", schedule="overlap",
        approved_oracle_id=oracle_id,
    )
    assert asyncio.run(process_one(store, "export-test-worker"))
    run = store.get(UUID(admitted["run_id"]))
    assert run["status"] == "COMPLETE"
    assert run["verdict"] == "FAIL"
    return oracle_id, run


def test_export_sha_is_a_current_immutable_idempotent_record(export_lab):
    store = export_lab
    oracle_id, run = _certified_run(store)
    url = f"/api/runs/{run['run_id']}"
    headers = {"X-Operator-Token": "immutable-export-test-secret"}
    with TestClient(create_app()) as client:
        assert client.post(f"{url}/exports").status_code == 403
        assert client.get(f"{url}/exports").json()["exports"] == []
        downloaded = client.get(f"{url}/witness")
        assert downloaded.status_code == 200
        assert downloaded.json() == run["witness"]
        # A plain GET downloads evidence without mutating the export journal.
        initial_bundle = client.get(f"{url}/bundle")
        assert initial_bundle.status_code == 200
        assert client.get(f"{url}/exports").json()["exports"] == []

        first = client.post(f"{url}/exports", headers=headers)
        assert first.status_code == 201, first.text
        record = first.json()
        assert record["current"] is True
        assert record["scope"] == "SYNTHETIC_ONLY"
        assert record["run_version"] == run["version"]
        assert record["evidence_sha256"] == run["witness"]["evidence_sha256"]
        assert record["artifact_sha256"] == hashlib.sha256(
            initial_bundle.content
        ).hexdigest()
        assert record["artifact_bytes"] == len(initial_bundle.content)
        assert client.post(f"{url}/exports", headers=headers).json() == record

        fresh_bundle = client.get(f"{url}/bundle")
        assert fresh_bundle.status_code == 200
        assert fresh_bundle.content == initial_bundle.content
        assert fresh_bundle.headers["x-clashbolt-artifact-sha256"] == (
            record["artifact_sha256"]
        )
        assert asyncio.run(verify_bundle(fresh_bundle.content))["status"] == (
            "REPRODUCED"
        )
        history = client.get(f"{url}/exports").json()
        assert history["project_id"] == "local"
        assert len(history["exports"]) == 1
        assert history["exports"][0]["export_id"] == record["export_id"]

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with store.connect() as conn:
                conn.execute(
                    "UPDATE witness_export SET artifact_sha256 = %s WHERE id = %s",
                    ("0" * 64, UUID(record["export_id"])),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with store.connect() as conn:
                conn.execute(
                    "DELETE FROM witness_export WHERE id = %s",
                    (UUID(record["export_id"]),),
                )

        assert store.revoke_rule(oracle_id)
        history = client.get(f"{url}/exports").json()
        assert history["exports"][0]["current"] is False
        assert client.get(f"{url}/bundle").status_code == 409
        assert client.get(f"{url}/witness").status_code == 409
        assert client.post(f"{url}/exports", headers=headers).status_code == 409
        assert client.get(f"/api/runs/{uuid4()}/exports").status_code == 404
