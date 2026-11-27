"""Typed local-only routes for oracle review and deterministic tool authorization."""

import hmac
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from clashbolt.experiment import execute
from clashbolt.model_planner import propose_model_plan
from clashbolt.oracle_workshop import RuleCandidate
from clashbolt.planner import choose_plan, compare_strategies
from clashbolt.policy import Policy


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposalInput(StrictModel):
    proposed_by: str = Field(default="deterministic-workshop", min_length=1, max_length=100)
    candidate: RuleCandidate | None = None


class ReviewInput(StrictModel):
    expected_hash: str = Field(min_length=64, max_length=64)
    reviewer: str = Field(min_length=1, max_length=100)


class CapabilityInput(StrictModel):
    proposal_id: UUID
    variant: Literal["racy", "locked"]
    schedule: Literal["overlap", "serial"]
    max_calls: int = Field(default=1, ge=1, le=10)
    lifetime_seconds: int = Field(default=600, ge=60, le=3600)
    plan_id: UUID | None = None


class ToolInput(StrictModel):
    variant: Literal["racy", "locked"]
    schedule: Literal["overlap", "serial"]
    plan_id: UUID


class PlanInput(StrictModel):
    proposal_id: UUID
    strategy: Literal["semantic", "random", "coverage"]
    variant: Literal["racy", "locked"]
    seed: int = Field(default=0, ge=0, le=1_000_000)


class LocalModelPlanInput(StrictModel):
    proposal_id: UUID
    variant: Literal["racy", "locked"]


class FeedbackInput(StrictModel):
    remaining_budget: int = Field(ge=0, le=32, strict=True)


class EvaluationInput(StrictModel):
    trials: int = Field(default=8, ge=1, le=20)


def require_operator(
    x_operator_token: str | None, http_request: Request | None = None
) -> None:
    """Require an operator identity in account mode, else the legacy local token."""
    if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts":
        account = (
            getattr(http_request.state, "account", None) if http_request else None
        )
        if not account or account["role"] != "operator":
            raise HTTPException(403, "Authenticated operator account required")
        return
    configured = os.getenv("CLASHBOLT_OPERATOR_TOKEN")
    if not configured or not x_operator_token:
        raise HTTPException(403, "Operator token is required")
    if not hmac.compare_digest(configured, x_operator_token):
        raise HTTPException(403, "Invalid operator token")


def require_reviewer(
    reviewer_token: str | None, operator_token: str | None,
    http_request: Request | None = None,
) -> None:
    """Named reviewer in account mode; distinct secret in legacy local mode."""
    if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts":
        account = (
            getattr(http_request.state, "account", None) if http_request else None
        )
        if not account or account["role"] != "reviewer":
            raise HTTPException(403, "Authenticated reviewer account required")
        return
    configured = os.getenv("CLASHBOLT_REVIEWER_TOKEN")
    if not configured:
        if os.getenv("CLASHBOLT_ACCESS_MODE") == "private":
            raise HTTPException(403, "Private mode requires a separate reviewer token")
        # Compatibility with single-operator local development mode.
        require_operator(operator_token)
        return
    if not reviewer_token or configured == os.getenv("CLASHBOLT_OPERATOR_TOKEN"):
        raise HTTPException(403, "Independent reviewer credential required")
    if not hmac.compare_digest(configured, reviewer_token):
        raise HTTPException(403, "Invalid reviewer token")


def policy_router(dsn: str | None) -> APIRouter:
    router = APIRouter(prefix="/api")
    policy = Policy(dsn) if dsn else None

    def require_policy(
        http_request: Request | None = None, *, capability_only: bool = False
    ) -> Policy:
        if not policy:
            raise HTTPException(503, "Set CLASHBOLT_DATABASE_URL to enable rule review")
        if os.getenv("CLASHBOLT_ACCESS_MODE") != "accounts":
            return policy
        if http_request is None:
            raise HTTPException(403, "Project context is required")
        if capability_only:
            selected = getattr(http_request.state, "project", None)
        else:
            account = getattr(http_request.state, "account", None)
            selected = (
                {"project_id": account["project_id"], "schema": account["schema"]}
                if account else None
            )
        if not selected or not selected.get("schema"):
            raise HTTPException(403, "Authorized project context is required")
        if capability_only:
            return Policy(
                dsn, schema=selected["schema"], project_id=selected["project_id"]
            )
        actor = http_request.state.account
        path = http_request.url.path
        reviewer_write = (
            http_request.method == "POST"
            and path.startswith("/api/oracle/proposals/")
            and (path.endswith("/approve") or path.endswith("/revoke"))
        )
        operator_only_read = (
            path in (
                "/api/oracle/audit", "/api/tool-audit", "/api/agent/model-budget",
            )
            or path.endswith("/history")
        )
        return Policy(
            dsn, schema=selected["schema"], project_id=selected["project_id"],
            actor_account_id=UUID(actor["id"]), actor_session_epoch=actor["epoch"],
            required_role=(
                "reviewer" if reviewer_write else "operator"
                if http_request.method not in ("GET", "HEAD", "OPTIONS")
                    or operator_only_read
                else "viewer"
            ),
        )


    @router.post("/oracle/proposals", status_code=201)
    def propose_rule(request: ProposalInput, http_request: Request):
        actor = (
            http_request.state.account["username"]
            if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts"
            else request.proposed_by
        )
        return require_policy(http_request).propose(actor, request.candidate)

    @router.get("/oracle/audit")
    def list_oracle_audit(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        return {"events": require_policy(http_request).oracle_audit()}

    @router.get("/oracle/proposals")
    def list_rules(http_request: Request):
        return {"proposals": require_policy(http_request).list_proposals()}

    @router.post("/oracle/proposals/{proposal_id}/approve")
    def approve_rule(
        proposal_id: UUID, request: ReviewInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
        x_reviewer_token: str | None = Header(default=None),
    ):
        require_reviewer(x_reviewer_token, x_operator_token, http_request)
        reviewer = (
            http_request.state.account["username"]
            if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts"
            else request.reviewer
        )
        try:
            return require_policy(http_request).approve(
                proposal_id, request.expected_hash, reviewer
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/oracle/proposals/{proposal_id}/revoke")
    def revoke_rule(
        proposal_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
        x_reviewer_token: str | None = Header(default=None),
    ):
        require_reviewer(x_reviewer_token, x_operator_token, http_request)
        actor = (
            http_request.state.account["username"]
            if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts"
            else "local-reviewer"
        )
        if not require_policy(http_request).revoke_rule(proposal_id, actor=actor):
            raise HTTPException(409, "Oracle version is not currently approved")
        return {"proposal_id": str(proposal_id), "state": "REVOKED"}

    @router.post("/tool-capabilities", status_code=201)
    def issue_capability(
        request: CapabilityInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        try:
            issuer = (
                UUID(http_request.state.account["id"])
                if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts"
                else None
            )
            return require_policy(http_request).issue(
                request.proposal_id, request.variant, request.schedule,
                request.max_calls, request.lifetime_seconds, request.plan_id,
                issuer_account_id=issuer,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/tool-capabilities/{capability_id}/revoke")
    def revoke_capability(
        capability_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        if not require_policy(http_request).revoke_capability(capability_id):
            raise HTTPException(404, "No active capability with this ID")
        return {"capability_id": str(capability_id), "revoked": True}

    @router.get("/tool-audit")
    def tool_audit(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        return {"events": require_policy(http_request).audit()}

    @router.post("/agent/plans", status_code=201)
    def propose_plan(request: PlanInput, http_request: Request):
        try:
            plan = choose_plan(
                strategy=request.strategy, variant=request.variant, seed=request.seed
            )
            return require_policy(http_request).record_plan(request.proposal_id, plan)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.get("/agent/frontier/{proposal_id}")
    def get_frontier(
        proposal_id: UUID, http_request: Request,
        variant: Literal["racy", "locked"] = "racy",
    ):
        try:
            return require_policy(http_request).frontier(proposal_id, variant)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/agent/frontier/{proposal_id}/plans", status_code=201)
    def create_frontier_plan(
        proposal_id: UUID, request: LocalModelPlanInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        if proposal_id != request.proposal_id:
            raise HTTPException(409, "Frontier and oracle proposal IDs differ")
        try:
            return require_policy(http_request).propose_frontier_plan(proposal_id, request.variant)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/agent/plans/model", status_code=201)
    async def propose_local_model_plan(
        request: LocalModelPlanInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        store = require_policy(http_request)
        try:
            store.frontier(request.proposal_id, request.variant)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        # No model configuration means zero provider work and zero reservation.
        reservation = None
        if os.getenv("CLASHBOLT_LOCAL_MODEL"):
            try:
                reservation = store.reserve_model_call()
            except ValueError as error:
                raise HTTPException(429, str(error)) from error
        try:
            plan = await propose_model_plan(request.variant)
        except Exception:
            if reservation is not None:
                store.settle_model_call(reservation, None)
            raise
        if reservation is not None:
            usage = (
                plan.get("model_tokens_used")
                if plan.get("model_status") == "VALIDATED_PROPOSAL"
                else None
            )
            settled = store.settle_model_call(reservation, usage)
            if plan.get("model_status") == "VALIDATED_PROPOSAL" and (
                not settled
                or store.model_reservation_state(reservation) != "COMPLETE"
            ):
                # A late model suggestion is never upgraded into an agent plan.
                plan = {
                    **choose_plan(strategy="semantic", variant=request.variant),
                    "planner_kind": "DETERMINISTIC_FALLBACK",
                    "model_status": "LATE_OR_UNCONFIRMED_RESERVATION",
                }
        try:
            return store.record_plan(request.proposal_id, plan)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/agent/tools/run")
    async def execute_tool(
        request: ToolInput, http_request: Request,
        authorization: str | None = Header(default=None),
    ):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(403, "A scoped tool capability is required")
        secret = authorization.removeprefix("Bearer ")
        policy = require_policy(http_request, capability_only=True)
        if len(secret) > 256 or not policy.authorize(
            secret, request.variant, request.schedule, request.plan_id,
            accounts_mode=os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts",
        ):
            raise HTTPException(403, "Tool action denied by policy")
        # This is the only allowed tool and only uses the shipped fixture.
        witness = await execute(request.variant, request.schedule)
        if not policy.record_outcome(request.plan_id, witness):
            raise HTTPException(409, "A validated plan receipt could not be stored")
        adjudicated = policy.get_plan(request.plan_id)
        if not adjudicated or adjudicated["state"] != "COMPLETE":
            raise HTTPException(409, "Tool receipt was not certifiable")
        return witness

    @router.get("/agent/plans/{plan_id}")
    def get_plan(plan_id: UUID, http_request: Request):
        result = require_policy(http_request).get_plan(plan_id)
        if not result:
            raise HTTPException(404, "Plan not found")
        return result

    @router.get("/agent/plans/{plan_id}/history")
    def read_plan_history(
        plan_id: UUID, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        record = require_policy(http_request).plan_history(plan_id)
        if not record:
            raise HTTPException(404, "Plan not found")
        return record

    @router.get("/agent/plans/{plan_id}/gaps")
    def read_evidence_gaps(plan_id: UUID, http_request: Request):
        store = require_policy(http_request)
        return {
            "gaps": store.evidence_gaps(plan_id),
            "uncertainty": store.plan_uncertainty(plan_id),
        }

    @router.get("/agent/plans/{plan_id}/feedback")
    def read_feedback(plan_id: UUID, http_request: Request):
        record = require_policy(http_request).get_feedback(plan_id)
        if not record:
            raise HTTPException(404, "No completed feedback for this plan")
        return record

    @router.post("/agent/plans/{plan_id}/feedback", status_code=201)
    def record_feedback(
        plan_id: UUID, request: FeedbackInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        try:
            return require_policy(http_request).record_feedback(plan_id, request.remaining_budget)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/agent/plans/reconcile")
    def reconcile_plans(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        return {"reconciled": require_policy(http_request).reconcile_stalled_plans()}

    @router.get("/agent/model-budget")
    def model_budget(
        http_request: Request, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        return require_policy(http_request).local_model_budget()

    @router.post("/agent/evaluate")
    async def evaluate(
        request: EvaluationInput, http_request: Request,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token, http_request)
        require_policy(http_request)
        return await compare_strategies(request.trials)

    return router
