"""ClashBolt API: local scratch lab and optional PostgreSQL durable run control."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import OrderedDict
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from psycopg import Error as DatabaseError
from pydantic import BaseModel, ConfigDict, Field

from clashbolt.accounts import (
    ACCOUNT_SCHEMA_VERSION,
    AccountStore,
    sign_session,
    verify_session,
)
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
from clashbolt.policy import POLICY_SCHEMA_VERSION
from clashbolt.reducer import reduce_witness
from clashbolt.routes_policy import policy_router, require_operator
from clashbolt.store import BASE_SCHEMA_VERSION, Ledger
from clashbolt.witness import verify
from clashbolt.witness_bundle import create_bundle


def _signed_session(secret: str) -> str:
    payload = f"{int(time.time()) + 3600}.{secrets.token_urlsafe(18)}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def _valid_session(cookie: str | None, secret: str | None) -> bool:
    if not cookie or not secret:
        return False
    try:
        expiry, nonce, digest = cookie.split(".", 2)
        if len(nonce) > 64 or len(digest) != 64 or not expiry.isdigit():
            return False
        if not int(time.time()) < int(expiry) <= int(time.time()) + 3600:
            return False
        payload = f"{expiry}.{nonce}"
        correct = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(correct, digest)
    except (TypeError, ValueError):
        return False


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: Literal["synthetic-inventory"] = "synthetic-inventory"
    variant: Literal["racy", "locked"]
    schedule: Literal["overlap", "serial"]


class AccountLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=48)
    password: str = Field(min_length=1, max_length=256)


class DraftRequest(ExperimentRequest):
    expected_version: int = Field(ge=0, le=2**63 - 2)
    execution_backend: Literal["memory", "native-postgresql"] = "memory"


class RunRequest(ExperimentRequest):
    client_key: str = Field(min_length=1, max_length=100)
    step_budget: int = Field(default=4, ge=4, le=32)
    approved_oracle_id: UUID
    execution_backend: Literal["memory", "native-postgresql"] = "memory"


def create_app() -> FastAPI:
    app = FastAPI(title="ClashBolt Synthetic Lab", version="0.3.0")
    access_mode = os.getenv("CLASHBOLT_ACCESS_MODE", "local")
    if access_mode not in ("local", "private", "accounts"):
        raise ValueError("Unsupported CLASHBOLT_ACCESS_MODE")
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    account_store = AccountStore(dsn) if access_mode == "accounts" and dsn else None
    session_secret = os.getenv("CLASHBOLT_SESSION_SECRET", "")
    if access_mode == "accounts" and (
        account_store is None or len(session_secret) < 32
    ):
        raise ValueError("Account mode needs database and a 32-character session secret")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Content-Type", "Authorization", "Last-Event-ID",
            "X-Operator-Token", "X-Reviewer-Token",
        ],
        allow_credentials=True,
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

    @app.middleware("http")
    async def private_access(request: Request, call_next):
        if (
            os.getenv("CLASHBOLT_ACCESS_MODE") != "private"
            or not request.url.path.startswith("/api/")
            or request.url.path in ("/api/health", "/api/ready", "/api/catalog", "/api/session")
        ):
            return await call_next(request)
        # Review authority is independent: a reviewer must not need an
        # operator cookie or execution secret to approve or revoke a rule.
        path = request.url.path
        reviewing = (
            request.method == "POST"
            and path.startswith("/api/oracle/proposals/")
            and (path.endswith("/approve") or path.endswith("/revoke"))
        )
        if reviewing:
            reviewer_secret = os.getenv("CLASHBOLT_REVIEWER_TOKEN")
            operator_secret = os.getenv("CLASHBOLT_OPERATOR_TOKEN")
            presented = request.headers.get("X-Reviewer-Token")
            if (
                not reviewer_secret
                or reviewer_secret == operator_secret
                or not presented
                or not hmac.compare_digest(reviewer_secret, presented)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Independent reviewer credential required"},
                )
            return await call_next(request)
        secret = os.getenv("CLASHBOLT_OPERATOR_TOKEN")
        if not _valid_session(request.cookies.get("clashbolt_session"), secret):
            return JSONResponse(
                status_code=401, content={"detail": "Private operator session required"}
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                require_operator(request.headers.get("X-Operator-Token"))
            except HTTPException:
                return JSONResponse(
                    status_code=403, content={"detail": "Operator token required"}
                )
        return await call_next(request)

    @app.middleware("http")
    async def account_access(request: Request, call_next):
        if access_mode != "accounts" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        path = request.url.path
        if path in ("/api/health", "/api/ready", "/api/catalog", "/api/auth/login"):
            return await call_next(request)
        if path == "/api/session":
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        # A separately issued, bounded capability authorizes the one fixed tool.
        if path == "/api/agent/tools/run" and request.method == "POST":
            return await call_next(request)
        identity = verify_session(
            request.cookies.get("clashbolt_identity"), session_secret, account_store
        )
        if identity is None:
            return JSONResponse(
                status_code=401, content={"detail": "Named account session required"},
            )
        request.state.account = identity
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            reviewing = (
                request.method == "POST"
                and path.startswith("/api/oracle/proposals/")
                and (path.endswith("/approve") or path.endswith("/revoke"))
            )
            permitted = (
                path == "/api/auth/logout"
                or (reviewing and identity["role"] == "reviewer")
                or (not reviewing and identity["role"] == "operator")
            )
            if not permitted:
                return JSONResponse(
                    status_code=403, content={"detail": "Account role is not authorized"},
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/login")
    def account_login(request: Request, credentials: AccountLogin):
        if account_store is None:
            raise HTTPException(404, "Named accounts are not enabled")
        identity = account_store.authenticate(
            credentials.username, credentials.password
        )
        if identity is None:
            raise HTTPException(401, "Invalid account credentials")
        response = JSONResponse(content={
            "username": identity["username"], "role": identity["role"],
            "expires_in": 3600,
        })
        response.set_cookie(
            key="clashbolt_identity",
            value=sign_session(identity, session_secret),
            httponly=True, samesite="strict", path="/api", max_age=3600,
            secure=request.url.scheme == "https",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/me")
    def account_me(request: Request):
        if account_store is None:
            raise HTTPException(404, "Named accounts are not enabled")
        identity = request.state.account
        return {"username": identity["username"], "role": identity["role"]}

    @app.post("/api/auth/logout")
    def account_logout(request: Request):
        if account_store is None:
            raise HTTPException(404, "Named accounts are not enabled")
        account_store.revoke_sessions(UUID(request.state.account["id"]))
        response = JSONResponse(content={"status": "LOGGED_OUT"})
        response.delete_cookie("clashbolt_identity", path="/api")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/session")
    def unlock_session(
        request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        response = JSONResponse(content={"status": "UNLOCKED", "expires_in": 3600})
        response.set_cookie(
            key="clashbolt_session",
            value=_signed_session(x_operator_token),
            max_age=3600, httponly=True, samesite="strict",
            path="/api", secure=request.url.scheme == "https",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # Scratch-only compatibility is intentionally bounded and volatile.
    runs: OrderedDict[str, dict] = OrderedDict()
    max_scratch_runs = 50
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

    @app.get("/api/ready")
    def ready() -> dict:
        """Readiness does not create schema or hide failed dependencies."""
        if ledger is None:
            return {"status": "ready", "storage": "volatile-scratch-only"}
        try:
            with ledger.connect() as connection:
                revisions = connection.execute(
                    "SELECT component, version FROM schema_revision"
                ).fetchall()
                installed = {r["component"]: r["version"] for r in revisions}
                required = {
                    "control": BASE_SCHEMA_VERSION,
                    "policy": POLICY_SCHEMA_VERSION,
                }
                if access_mode == "accounts":
                    required["accounts"] = ACCOUNT_SCHEMA_VERSION
                if any(installed.get(key) != expected for key, expected in required.items()):
                    raise HTTPException(
                        503, "Database schema is missing or incompatible"
                    )
                if access_mode == "accounts":
                    active = connection.execute(
                        "SELECT count(*) AS n FROM local_account WHERE active = TRUE"
                    ).fetchone()["n"]
                    if active == 0:
                        raise HTTPException(503, "No active named accounts are configured")
        except DatabaseError as error:
            raise HTTPException(503, "Control database is unavailable") from error
        return {"status": "ready", "storage": "postgresql", "access_mode": access_mode}

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
            "access_mode": access_mode,
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
        if len(runs) > max_scratch_runs:
            runs.popitem(last=False)
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

    @app.get("/api/experiment-draft")
    def get_experiment_draft() -> dict:
        return {"saved": require_ledger().get_draft()}

    @app.post("/api/experiment-draft")
    def save_experiment_draft(
        request: DraftRequest, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        try:
            return require_ledger().save_draft(
                {
                    "variant": request.variant,
                    "schedule": request.schedule,
                    "execution_backend": request.execution_backend,
                },
                request.expected_version,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/runs")
    def list_runs(limit: int = 20) -> dict:
        try:
            return {"runs": require_ledger().recent(limit)}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/runs", status_code=201)
    def submit_run(
        request: RunRequest, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
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
    def get_events(run_id: UUID, after: int = 0, limit: int = 100) -> dict:
        if after < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid event cursor or limit")
        store = require_ledger()
        if not store.get(run_id):
            raise HTTPException(404, "Run not found")
        return {
            "run_id": str(run_id),
            **store.transition_page(run_id, after=after, limit=limit),
        }

    @app.get("/api/runs/{run_id}/stream")
    async def stream_events(request: Request, run_id: UUID, after: int = 0):
        store = require_ledger()
        if not store.get(run_id):
            raise HTTPException(404, "Run not found")
        resume = request.headers.get("Last-Event-ID")
        if resume is not None:
            if not resume.isascii() or not resume.isdigit() or len(resume) > 18:
                raise HTTPException(422, "Malformed Last-Event-ID")
            after = max(after, int(resume))
        if after < 0:
            raise HTTPException(422, "Cursor cannot be negative")

        async def messages():
            cursor = after
            # Streaming is advisory. Reconnect and GET the canonical snapshot.
            for _ in range(30):
                if await request.is_disconnected():
                    break
                # Replay all pending pages before waiting for another hint.
                for _ in range(20):
                    page = store.transition_page(run_id, cursor)
                    for event in page["events"]:
                        cursor = event["version"]
                        payload = json.dumps(event, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: transition\ndata: {payload}\n\n"
                    if not page["has_more"]:
                        break
                await asyncio.sleep(1)

        return StreamingResponse(
            messages(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(
        run_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        result = require_ledger().cancel(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        return result

    @app.get("/api/runs/{run_id}/bundle")
    def download_witness_bundle(run_id: UUID):
        current = require_ledger().get(run_id)
        if not current:
            raise HTTPException(404, "Run not found")
        if (
            current["status"] != "COMPLETE"
            or current["verdict"] not in ("PASS", "FAIL")
            or not current["witness"]
        ):
            raise HTTPException(409, "No currently certified witness to package")
        try:
            data = create_bundle(current["witness"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        # Read-only evidence package; no new execution or side effects.
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="clashbolt-witness-{run_id}.zip"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/runs/{run_id}/witness")
    def download_witness(run_id: UUID) -> dict:
        result = require_ledger().get(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        if result["witness"] is None:
            raise HTTPException(409, "No complete witness exists yet")
        return result["witness"]

    @app.post("/api/runs/{run_id}/reduce")
    async def minimize_witness(
        run_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        current = require_ledger().get(run_id)
        if not current or current["status"] != "COMPLETE" or current["verdict"] != "FAIL":
            raise HTTPException(409, "Only current approved failures can be reduced")
        cached = require_ledger().get_reduction(run_id)
        if cached:
            return cached
        report = await asyncio.to_thread(
            reduce_witness,
            current["witness"],
            os.getenv("CLASHBOLT_TARGET_ADMIN_URL"),
            dsn,
        )
        if report["status"] not in ("MINIMAL", "BOUNDED"):
            raise HTTPException(409, report.get("reason", "Reduction not confirmed"))
        saved = require_ledger().record_reduction(
            run_id, current["version"], report
        )
        if not saved:
            raise HTTPException(409, "Run verdict changed during reduction")
        return saved

    @app.get("/api/runs/{run_id}/reduction")
    def read_reduction(run_id: UUID) -> dict:
        saved = require_ledger().get_reduction(run_id)
        if not saved:
            raise HTTPException(404, "No current reduction report")
        return saved

    @app.post("/api/runs/{run_id}/replay")
    async def replay_durable_run(
        run_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
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
