"""ClashBolt API: local scratch lab and optional PostgreSQL durable run control."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
from clashbolt.metrics import operational_metrics
from clashbolt.ops_alerts import diagnose_operations
from clashbolt.policy import POLICY_SCHEMA_VERSION
from clashbolt.reducer import reduce_witness
from clashbolt.routes_policy import policy_router, require_operator
from clashbolt.store import BASE_SCHEMA_VERSION, Ledger
from clashbolt.verification_jobs import VerificationJobs
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


class ProjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=3, max_length=48)


class DraftRequest(ExperimentRequest):
    expected_version: int = Field(ge=0, le=2**63 - 2)
    execution_backend: Literal["memory", "native-postgresql"] = "memory"


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["REPLAY", "REDUCE"]


class RunRequest(ExperimentRequest):
    client_key: str = Field(min_length=1, max_length=100)
    step_budget: int = Field(default=4, ge=4, le=32)
    approved_oracle_id: UUID
    execution_backend: Literal["memory", "native-postgresql"] = "memory"


def create_app() -> FastAPI:
    app = FastAPI(title="ClashBolt Synthetic Lab", version="0.3.0")
    dist_path = os.getenv("CLASHBOLT_FRONTEND_DIST")
    frontend_dir = Path(dist_path).resolve() if dist_path else None
    if frontend_dir and (
        not (frontend_dir / "index.html").is_file()
        or not (frontend_dir / "assets").is_dir()
    ):
        raise ValueError("CLASHBOLT_FRONTEND_DIST must be a built Vite distribution")
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
    # The bundled UI is same-origin: never trust a separate dev-server origin
    # when the hardened static deployment is enabled.
    dev_origins = ("http://localhost:5173", "http://127.0.0.1:5173")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[] if frontend_dir else list(dev_origins),
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
        raw_host = request.headers.get("host", "")
        try:
            parsed_host = urlsplit("//" + raw_host)
            # URL parsing alone accepts invalid Host forms such as
            # "user@localhost", "localhost/path", or a nonnumeric port.
            _ = parsed_host.port
            requested_host = (
                parsed_host.hostname
                if not (
                    parsed_host.username is not None
                    or parsed_host.password is not None
                    or parsed_host.path or parsed_host.query or parsed_host.fragment
                )
                else None
            )
        except ValueError:
            requested_host = None
        if (
            peer not in ("127.0.0.1", "::1", "testclient")
            or requested_host not in ("127.0.0.1", "::1", "localhost", "testserver")
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "ClashBolt is restricted to local connections"},
            )
        origin = request.headers.get("origin")
        trusted_origin = (
            origin == f"{request.url.scheme}://{raw_host}"
            if frontend_dir
            else origin in (
                *dev_origins, "http://127.0.0.1:8000", "http://localhost:8000",
            )
        )
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and origin is not None
            and not trusted_origin
        ):
            return JSONResponse(
                status_code=403, content={"detail": "Cross-origin write was denied"},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if frontend_dir:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        if not request.url.path.endswith("/stream"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def private_access(request: Request, call_next):
        if (
            os.getenv("CLASHBOLT_ACCESS_MODE") != "private"
            or not request.url.path.startswith("/api/")
            or request.url.path in ("/api/health", "/api/ready", "/api/catalog", "/api/session")
        ):
            return await call_next(request)
        # Capability-only tool dispatch is independently authorized by the
        # broker. It must not also require an operator session or token.
        path = request.url.path
        if path == "/api/agent/tools/run" and request.method == "POST":
            return await call_next(request)
        # Review authority is independent: a reviewer must not need an
        # operator cookie or execution secret to approve or revoke a rule.
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

        # The HttpOnly project cookie is a *selector*, never authority.
        # All named API endpoints require a live project membership; the
        # one capability-only endpoint independently checks the issuer.
        project_id = request.cookies.get("clashbolt_project", "local")
        schema = account_store.project_schema(project_id)
        if path == "/api/agent/tools/run" and request.method == "POST":
            if schema is None:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Unknown tool capability project"},
                )
            request.state.project = {"project_id": project_id, "schema": schema}
            return await call_next(request)

        identity = verify_session(
            request.cookies.get("clashbolt_identity"), session_secret, account_store
        )
        if identity is None:
            return JSONResponse(
                status_code=401, content={"detail": "Named account session required"},
            )
        account_id = UUID(identity["id"])
        # Project selection, listing and logout remain usable when the current
        # project was revoked; they do not access project resource tables.
        account_only = path in (
            "/api/auth/logout", "/api/auth/projects", "/api/auth/select-project"
        )
        effective_role = (
            account_store.project_role(account_id, project_id)
            if schema is not None else None
        )
        if not account_only and effective_role is None:
            return JSONResponse(
                status_code=403,
                content={"detail": "No permission for the selected project"},
            )
        request.state.account = {
            **identity, "role": effective_role,
            "project_id": project_id, "schema": schema,
        }
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            reviewing = (
                request.method == "POST"
                and path.startswith("/api/oracle/proposals/")
                and (path.endswith("/approve") or path.endswith("/revoke"))
            )
            permitted = (
                account_only
                or (reviewing and effective_role == "reviewer")
                or (not reviewing and effective_role == "operator")
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
        peer = request.client.host if request.client else ""
        if not account_store.reserve_login_attempt(peer):
            raise HTTPException(
                429, "Too many login attempts; retry later",
                headers={"Retry-After": "60"},
            )
        identity = account_store.authenticate(
            credentials.username, credentials.password
        )
        if identity is None:
            raise HTTPException(401, "Invalid account credentials")
        projects = account_store.accessible_projects(UUID(identity["id"]))
        if not projects:
            raise HTTPException(403, "No project permission is configured")
        selected = next(
            (project for project in projects if project["id"] == "local"),
            projects[0],
        )
        response = JSONResponse(content={
            "username": identity["username"], "role": selected["role"],
            "project_id": selected["id"], "expires_in": 3600,
        })
        response.set_cookie(
            key="clashbolt_identity",
            value=sign_session(identity, session_secret),
            httponly=True, samesite="strict", path="/api", max_age=3600,
            secure=request.url.scheme == "https",
        )
        response.set_cookie(
            key="clashbolt_project", value=selected["id"],
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
        return {
            "username": identity["username"], "role": identity["role"],
            "project_id": identity["project_id"],
        }

    @app.get("/api/auth/projects")
    def account_projects(request: Request) -> dict:
        identity = request.state.account
        return {
            "projects": account_store.accessible_projects(UUID(identity["id"])),
            "selected_project_id": identity["project_id"],
        }

    @app.post("/api/auth/select-project")
    def account_select_project(
        selection: ProjectSelection, request: Request
    ):
        identity = request.state.account
        available = account_store.accessible_projects(UUID(identity["id"]))
        selected = next(
            (project for project in available if project["id"] == selection.project_id),
            None,
        )
        if not selected:
            raise HTTPException(403, "No grant for selected project")
        response = JSONResponse(content={
            "username": identity["username"], "role": selected["role"],
            "project_id": selected["id"],
        })
        response.set_cookie(
            key="clashbolt_project", value=selected["id"],
            httponly=True, samesite="strict", path="/api", max_age=3600,
            secure=request.url.scheme == "https",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/logout")
    def account_logout(request: Request):
        if account_store is None:
            raise HTTPException(404, "Named accounts are not enabled")
        account_store.revoke_sessions(UUID(request.state.account["id"]))
        response = JSONResponse(content={"status": "LOGGED_OUT"})
        response.delete_cookie("clashbolt_identity", path="/api")
        response.delete_cookie("clashbolt_project", path="/api")
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

    def require_ledger(http_request: Request | None = None) -> Ledger:
        if ledger is None:
            raise HTTPException(503, "Set CLASHBOLT_DATABASE_URL for durable runs")
        if access_mode != "accounts":
            return ledger
        account = (
            getattr(http_request.state, "account", None) if http_request else None
        )
        if account is None or not account.get("schema"):
            raise HTTPException(403, "An authorized project context is required")
        return Ledger(
            dsn, schema=account["schema"], project_id=account["project_id"]
        )

    def require_verifications(http_request: Request) -> VerificationJobs:
        scoped = require_ledger(http_request)
        return VerificationJobs(
            scoped.dsn, schema=scoped.schema, project_id=scoped.project_id
        )

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "mode": "synthetic-local",
            "storage": "postgresql" if ledger else "memory-only",
        }

    @app.get("/api/ready")
    def ready(require_worker: bool = False) -> dict:
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
                        """
                        SELECT count(*) AS n FROM local_account a
                        JOIN project_membership m ON m.account_id = a.id
                        JOIN project_workspace w ON w.project_id = m.project_id
                        WHERE a.active = TRUE
                          AND (m.role = 'viewer' OR m.role = a.role)
                        """
                    ).fetchone()["n"]
                    if active == 0:
                        raise HTTPException(
                            503, "No active project account grants are configured"
                        )
        except DatabaseError as error:
            raise HTTPException(503, "Control database is unavailable") from error
        if require_worker:
            try:
                worker_count = ledger.operations()["responsive_workers"]
            except DatabaseError as error:
                raise HTTPException(503, "Worker liveness is unavailable") from error
            if worker_count < 1:
                raise HTTPException(503, "No responsive worker heartbeat")
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
                if dsn and (
                    os.getenv("CLASHBOLT_TARGET_CLUSTER_URL")
                    or os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
                )
                else ["memory"]
            ),
            "limitations": (
                "Local synthetic fixture only; no external targets, "
                "network isolation, or human-approved policy"
            ),
        }

    @app.post("/api/experiments", status_code=201)
    async def create_experiment(request: ExperimentRequest) -> dict:
        # A process-global scratch cache cannot enforce project isolation.
        if access_mode == "accounts":
            raise HTTPException(404, "Use durable project-scoped runs")
        witness = await execute(request.variant, request.schedule)
        run_id = str(uuid4())
        result = {"run_id": run_id, "witness": witness, "replay_status": "NOT_RUN"}
        runs[run_id] = result
        if len(runs) > max_scratch_runs:
            runs.popitem(last=False)
        return result

    @app.get("/api/experiments/{run_id}")
    def get_experiment(run_id: str) -> dict:
        if access_mode == "accounts":
            raise HTTPException(404, "Use durable project-scoped runs")
        if run_id not in runs:
            raise HTTPException(404, "Experiment not found (state is in-memory)")
        return runs[run_id]

    @app.post("/api/experiments/{run_id}/replay")
    async def replay_experiment(run_id: str) -> dict:
        if access_mode == "accounts":
            raise HTTPException(404, "Use durable project-scoped runs")
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

    @app.get("/api/operations")
    def operations(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        snapshot = require_ledger(http_request).operations()
        return {**snapshot, "diagnostics": diagnose_operations(snapshot)}

    @app.get("/api/metrics")
    def metrics(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        return Response(
            operational_metrics(require_ledger(http_request).operations()),
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/experiment-draft")
    def get_experiment_draft(http_request: Request) -> dict:
        if access_mode == "accounts":
            account_id = UUID(http_request.state.account["id"])
            return {"saved": account_store.get_account_draft(
                account_id, http_request.state.account["project_id"]
            )}
        return {"saved": require_ledger(http_request).get_draft()}

    @app.post("/api/experiment-draft")
    def save_experiment_draft(
        request: DraftRequest, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        draft = {
            "variant": request.variant,
            "schedule": request.schedule,
            "execution_backend": request.execution_backend,
        }
        try:
            if access_mode == "accounts":
                account_id = UUID(http_request.state.account["id"])
                return account_store.save_account_draft(
                    account_id, draft, request.expected_version,
                    http_request.state.account["project_id"],
                )
            return require_ledger(http_request).save_draft(draft, request.expected_version)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/runs")
    def list_runs(http_request: Request, limit: int = 20) -> dict:
        try:
            return {"runs": require_ledger(http_request).recent(limit)}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/runs", status_code=201)
    def submit_run(
        request: RunRequest, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        store = require_ledger(http_request)
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
    def get_run(run_id: UUID, http_request: Request) -> dict:
        run = require_ledger(http_request).get(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/api/runs/{run_id}/events")
    def get_events(
        run_id: UUID, http_request: Request, after: int = 0, limit: int = 100
    ) -> dict:
        if after < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid event cursor or limit")
        store = require_ledger(http_request)
        if not store.get(run_id):
            raise HTTPException(404, "Run not found")
        return {
            "run_id": str(run_id),
            **store.transition_page(run_id, after=after, limit=limit),
        }

    @app.get("/api/runs/{run_id}/stream")
    async def stream_events(request: Request, run_id: UUID, after: int = 0):
        store = require_ledger(request)
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
        result = require_ledger(http_request).cancel(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        return result

    @app.get("/api/runs/{run_id}/bundle")
    def download_witness_bundle(run_id: UUID, http_request: Request):
        current = require_ledger(http_request).get(run_id)
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
    def download_witness(run_id: UUID, http_request: Request) -> dict:
        result = require_ledger(http_request).get(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        if result["witness"] is None:
            raise HTTPException(409, "No complete witness exists yet")
        return result["witness"]

    @app.post("/api/runs/{run_id}/verification-jobs", status_code=202)
    def enqueue_verification(
        run_id: UUID, body: VerificationRequest, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        try:
            return require_verifications(http_request).submit(run_id, body.kind)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/verification-jobs/{job_id}")
    def read_verification(job_id: UUID, http_request: Request) -> dict:
        record = require_verifications(http_request).get(job_id)
        if record is None:
            raise HTTPException(404, "Verification job not found")
        return record

    @app.post("/api/runs/{run_id}/reduce")
    async def minimize_witness(
        run_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        current = require_ledger(http_request).get(run_id)
        if not current or current["status"] != "COMPLETE" or current["verdict"] != "FAIL":
            raise HTTPException(409, "Only current approved failures can be reduced")
        if current["execution_backend"] == "native-postgresql":
            raise HTTPException(409, "Native reductions require a worker verification job")
        cached = require_ledger(http_request).get_reduction(run_id)
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
        saved = require_ledger(http_request).record_reduction(
            run_id, current["version"], report
        )
        if not saved:
            raise HTTPException(409, "Run verdict changed during reduction")
        return saved

    @app.get("/api/runs/{run_id}/reduction")
    def read_reduction(run_id: UUID, http_request: Request) -> dict:
        saved = require_ledger(http_request).get_reduction(run_id)
        if not saved:
            raise HTTPException(404, "No current reduction report")
        return saved

    @app.post("/api/runs/{run_id}/replay")
    async def replay_durable_run(
        run_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> dict:
        require_operator(x_operator_token, http_request)
        original = require_ledger(http_request).get(run_id)
        if original is None:
            raise HTTPException(404, "Run not found")
        if original["execution_backend"] == "native-postgresql":
            raise HTTPException(409, "Native replay requires a worker verification job")
        witness = original["witness"]
        if witness is None:
            raise HTTPException(409, "Run has no replayable witness")
        proof = await verify(witness)
        latest = require_ledger(http_request).get(run_id)
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

    if frontend_dir:
        # API routes are registered first; only static build artifacts are served.
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return app


app = create_app()
