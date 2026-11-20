from clashbolt.api import create_app
from fastapi.testclient import TestClient


def test_catalog_and_witness_replay():
    with TestClient(create_app()) as client:
        catalog = client.get("/api/catalog").json()
        assert catalog["target_id"] == "synthetic-inventory"
        response = client.post("/api/experiments", json={"variant": "racy", "schedule": "overlap"})
        assert response.status_code == 201
        created = response.json()
        assert created["witness"]["verdict"] == "FAIL"
        run_id = created["run_id"]
        assert client.get(f"/api/experiments/{run_id}").status_code == 200
        replay = client.post(f"/api/experiments/{run_id}/replay")
        assert replay.status_code == 200
        assert replay.json()["replay_status"] == "REPRODUCED"
        assert client.get(f"/api/experiments/{run_id}").json()["replay_status"] == "REPRODUCED"


def test_closed_inputs_and_missing_runs():
    with TestClient(create_app()) as client:
        bad_target = client.post(
            "/api/experiments", json={"variant": "remote", "schedule": "overlap"}
        )
        bad_schedule = client.post(
            "/api/experiments", json={"variant": "racy", "schedule": "unknown"}
        )
        assert bad_target.status_code == 422
        assert bad_schedule.status_code == 422
        assert client.post(
            "/api/experiments",
            json={"variant": "racy", "schedule": "overlap", "url": "https://example.org"},
        ).status_code == 422
        assert client.get("/api/experiments/missing").status_code == 404
        assert client.post("/api/experiments/missing/replay").status_code == 404


def test_remote_direct_client_is_denied():
    with TestClient(create_app(), client=("198.51.100.23", 43111)) as client:
        assert client.get("/api/catalog").status_code == 403
        assert client.post(
            "/api/experiments", json={"variant": "racy", "schedule": "overlap"}
        ).status_code == 403



def test_private_mode_requires_operator_session_for_reads_and_writes(monkeypatch):
    from clashbolt.api import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "private")
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "local-private-secret")
    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/catalog").status_code == 200
        assert client.get("/api/experiments/fake").status_code == 401
        payload = {"variant": "racy", "schedule": "overlap"}
        assert client.post("/api/experiments", json=payload).status_code == 401
        assert client.post(
            "/api/session", headers={"X-Operator-Token": "wrong"}
        ).status_code == 403
        unlocked = client.post(
            "/api/session", headers={"X-Operator-Token": "local-private-secret"}
        )
        assert unlocked.status_code == 200
        assert "httponly" in unlocked.headers["set-cookie"].lower()
        assert client.post("/api/experiments", json=payload).status_code == 403
        accepted = client.post(
            "/api/experiments", json=payload,
            headers={"X-Operator-Token": "local-private-secret"},
        )
        assert accepted.status_code == 201
        assert client.get(
            f"/api/experiments/{accepted.json()['run_id']}"
        ).status_code == 200


def test_scratch_experiment_history_has_a_strict_bound():
    with TestClient(create_app()) as client:
        first = None
        latest = None
        for i in range(55):
            response = client.post(
                "/api/experiments", json={"variant": "locked", "schedule": "serial"}
            )
            assert response.status_code == 201
            if i == 0:
                first = response.json()["run_id"]
            latest = response.json()["run_id"]
        assert first is not None and latest is not None
        assert client.get(f"/api/experiments/{first}").status_code == 404
        assert client.get(f"/api/experiments/{latest}").status_code == 200


def test_volatile_readiness_reports_explicit_scope(monkeypatch):
    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "local")
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        response = client.get("/api/ready")
        assert response.status_code == 200
        assert response.json()["storage"] == "volatile-scratch-only"
