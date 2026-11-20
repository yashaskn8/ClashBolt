from fastapi.testclient import TestClient

from clashbolt.api import create_app


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
        assert client.post("/api/experiments", json={"variant": "remote", "schedule": "overlap"}).status_code == 422
        assert client.post("/api/experiments", json={"variant": "racy", "schedule": "unknown"}).status_code == 422
        assert client.post(
            "/api/experiments",
            json={"variant": "racy", "schedule": "overlap", "url": "https://example.org"},
        ).status_code == 422
        assert client.get("/api/experiments/missing").status_code == 404
        assert client.post("/api/experiments/missing/replay").status_code == 404
