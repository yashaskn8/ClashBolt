"""ClashBolt API: local scratch lab and optional PostgreSQL durable run control."""

import asyncio
import json
import os
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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
from clashbolt.routes_policy import policy_router, require_operator
from clashbolt.store import Ledger
from clashbolt.witness import verify


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: Literal["synthetic-inventory"] = "synthetic-inventory"
    variant: Literal["racy", "locked"]
    schedule: Literal["overlap", "serial"]


class RunRequest(ExperimentRequest):
    client_key: str = Field(min_length=1, max_length=100)
    step_budget: int = Field(default=4, ge=4, le=32)
    approved_oracle_id: UUID
    execution_backend: Literal["memory", "native-postgresql"] = "memory"


def create_app() -> FastAPI:
    app = FastAPI(title="ClashBolt Synthetic Lab", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Only the direct peer is considered. Reverse proxies must not expose
        # this unauthenticated development service to remote users.
        peer = request.client.host if request.client else None
        if peer not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse(
                status_code=403,
                content={"detail": "ClashBolt is restricted to local connections"},
            )
        return await call_next(request)

    runs: dict[str, dict] = {}
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    ledger = Ledger(dsn) if dsn else None
    app.include_router(policy_router(dsn))

    def require_ledger() -> Ledger:
        if ledger is None:
            raise HTTPException(503, "Set CLASHBOLT_DATABASE_URL for durable runs")
        return ledger

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "synthetic-local",
            "storage": "postgresql" if ledger else "memory-only",
        }

    @app.get("/api/catalog")
    def catalog() -> dict:
        return {
            "target_id": TARGET_ID,
            "target_revision": TARGET_REVISION,
            "fixture": FIXTURE,
            "fixture_hash": FIXTURE_HASH,
            "variants": ["racy", "locked"],
            "schedules": [
                {"id": name, "gates": list(gates)} for name, gates in SCHEDULES.items()
            ],
            "oracle": {
                "id": ORACLE_ID,
                "version": ORACLE_VERSION,
                "sha256": ORACLE_HASH,
                "rule": ORACLE_RULE,
                "authority": "fixed-demo-rule",
            },
            "storage": "postgresql" if ledger else "memory-only",
            "execution_backends": (
                ["memory", "native-postgresql"]
                if dsn and os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
                else ["memory"]
            ),
            "limitations": (
                "Local synthetic fixture only; no external targets, "
                "network isolation, or human-approved policy"
            ),
        }

    @app.post("/api/experiments", status_code=201)
    async def create_experiment(request: ExperimentRequest) -> dict:
        # Scratch-mode compatibility endpoint; not a durable queue or production API.
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

    @app.get("/api/runs")
    def list_runs(limit: int = 20) -> dict:
        try:
            return {"runs": require_ledger().recent(limit)}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/runs", status_code=201)
    def submit_run(
        request: RunRequest, x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token)
        store = require_ledger()
        try:
            return store.admit(
                client_key=request.client_key,
                variant=request.variant,
                schedule=request.schedule,
                step_budget=request.step_budget,
                approved_oracle_id=request.approved_oracle_id,
                execution_backend=request.execution_backend,
            )
        except ValueError as error:
            status = 429 if "quota" in str(error).lower() else 409
            raise HTTPException(status, str(error)) from error

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: UUID) -> dict:
        run = require_ledger().get(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: UUID, after: int = 0) -> dict:
        if after < 0:
            raise HTTPException(422, "Cursor cannot be negative")
        store = require_ledger()
        if not store.get(run_id):
            raise HTTPException(404, "Run not found")
        return {"run_id": str(run_id), "events": store.transitions(run_id, after)}

    @app.get("/api/runs/{run_id}/stream")
    async def stream_events(request: Request, run_id: UUID, after: int = 0):
        store = require_ledger()
        if not store.get(run_id):
            raise HTTPException(404, "Run not found")
        if after < 0:
            raise HTTPException(422, "Cursor cannot be negative")

        async def messages():
            cursor = after
            # Streaming is advisory. Reconnect and GET the canonical snapshot.
            for _ in range(30):
                if await request.is_disconnected():
                    break
                for event in store.transitions(run_id, cursor):
                    cursor = event["version"]
                    payload = json.dumps(event, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: transition\ndata: {payload}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            messages(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(
        run_id: UUID, x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token)
        result = require_ledger().cancel(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        return result

    @app.get("/api/runs/{run_id}/witness")
    def download_witness(run_id: UUID) -> dict:
        result = require_ledger().get(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        if result["witness"] is None:
            raise HTTPException(409, "No complete witness exists yet")
        return result["witness"]

    @app.post("/api/runs/{run_id}/replay")
    async def replay_durable_run(
        run_id: UUID, x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token)
        original = require_ledger().get(run_id)
        if original is None:
            raise HTTPException(404, "Run not found")
        witness = original["witness"]
        if witness is None:
            raise HTTPException(409, "Run has no replayable witness")
        proof = await verify(witness)
        latest = require_ledger().get(run_id)
        same = (
            proof["status"] == "REPRODUCED"
            and latest is not None
            and latest["version"] == original["version"]
            and latest["status"] == "COMPLETE"
            and latest["verdict"] in ("PASS", "FAIL")
        )
        return {
            "run_id": str(run_id),
            "replay_status": "REPRODUCED" if same else "UNCONFIRMED",
            "original_sha256": witness.get("evidence_sha256"),
            "replay_sha256": proof.get("replay_sha256"),
            "replay_verdict": proof.get("verdict") if same else "UNKNOWN",
            "scope": "SYNTHETIC_ONLY",
            "adjudicated_verdict": latest["verdict"] if latest else "UNKNOWN",
        }

    return app


app = create_app()
