"""Local-only demo API. This is intentionally not an authenticated production service."""

from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from clashbolt.experiment import (
    FIXTURE,
    FIXTURE_HASH,
    ORACLE_HASH,
    ORACLE_ID,
    ORACLE_RULE,
    ORACLE_VERSION,
    SCHEDULES,
    TARGET_ID,
    TARGET_REVISION,
    execute,
)


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: Literal["synthetic-inventory"] = "synthetic-inventory"
    variant: Literal["racy", "locked"]
    schedule: Literal["overlap", "serial"]


def create_app() -> FastAPI:
    app = FastAPI(title="ClashBolt Synthetic Lab", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    # Deliberately ephemeral: stage 2 adds PostgreSQL durable experiment state.
    runs: dict[str, dict] = {}

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "mode": "synthetic-local"}

    @app.get("/api/catalog")
    def catalog() -> dict:
        return {
            "target_id": TARGET_ID,
            "target_revision": TARGET_REVISION,
            "fixture": FIXTURE,
            "fixture_hash": FIXTURE_HASH,
            "variants": ["racy", "locked"],
            "schedules": [{"id": name, "gates": list(gates)} for name, gates in SCHEDULES.items()],
            "oracle": {
                "id": ORACLE_ID,
                "version": ORACLE_VERSION,
                "sha256": ORACLE_HASH,
                "rule": ORACLE_RULE,
                "authority": "fixed-demo-rule",
            },
            "limitations": "Local synthetic fixture only; no real target, database isolation, or human approval",
        }

    @app.post("/api/experiments", status_code=201)
    async def create_experiment(request: ExperimentRequest) -> dict:
        # This strict typed boundary rejects arbitrary target URLs, scripts and plans.
        witness = await execute(request.variant, request.schedule)
        run_id = str(uuid4())
        result = {"run_id": run_id, "witness": witness, "replay_status": "NOT_RUN"}
        runs[run_id] = result
        return result

    @app.get("/api/experiments/{run_id}")
    def get_experiment(run_id: str) -> dict:
        if run_id not in runs:
            raise HTTPException(404, "Experiment not found (state is in-memory)")
        return runs[run_id]

    @app.post("/api/experiments/{run_id}/replay")
    async def replay_experiment(run_id: str) -> dict:
        original = runs.get(run_id)
        if original is None:
            raise HTTPException(404, "Experiment not found (state is in-memory)")
        witness = original["witness"]
        replayed = await execute(witness["variant"], witness["schedule"])
        same = (
            witness["complete"]
            and replayed["complete"]
            and witness["evidence_sha256"] == replayed["evidence_sha256"]
        )
        original["replay_status"] = "REPRODUCED" if same else "UNCONFIRMED"
        return {
            "run_id": run_id,
            "replay_status": original["replay_status"],
            "original_sha256": witness["evidence_sha256"],
            "replay_sha256": replayed["evidence_sha256"],
            "replay_verdict": replayed["verdict"],
            "scope": "SYNTHETIC_ONLY",
        }

    return app


app = create_app()
