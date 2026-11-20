"""Typed local-only routes for oracle review and deterministic tool authorization."""

import hmac
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from clashbolt.experiment import execute
from clashbolt.planner import choose_plan, compare_strategies
from clashbolt.policy import Policy


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposalInput(StrictModel):
    proposed_by: str = Field(default="deterministic-workshop", min_length=1, max_length=100)


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


class EvaluationInput(StrictModel):
    trials: int = Field(default=8, ge=1, le=20)


def policy_router(dsn: str | None) -> APIRouter:
    router = APIRouter(prefix="/api")
    policy = Policy(dsn) if dsn else None

    def require_policy() -> Policy:
        if not policy:
            raise HTTPException(503, "Set CLASHBOLT_DATABASE_URL to enable rule review")
        return policy

    def require_operator(x_operator_token: str | None) -> None:
        configured = os.getenv("CLASHBOLT_OPERATOR_TOKEN")
        if not configured or not x_operator_token:
            raise HTTPException(403, "Operator token is required")
        if not hmac.compare_digest(configured, x_operator_token):
            raise HTTPException(403, "Invalid operator token")

    @router.post("/oracle/proposals", status_code=201)
    def propose_rule(request: ProposalInput):
        return require_policy().propose(request.proposed_by)

    @router.get("/oracle/proposals")
    def list_rules():
        return {"proposals": require_policy().list_proposals()}

    @router.post("/oracle/proposals/{proposal_id}/approve")
    def approve_rule(
        proposal_id: UUID, request: ReviewInput,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        try:
            return require_policy().approve(
                proposal_id, request.expected_hash, request.reviewer
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/oracle/proposals/{proposal_id}/revoke")
    def revoke_rule(
        proposal_id: UUID, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        if not require_policy().revoke_rule(proposal_id):
            raise HTTPException(409, "Oracle version is not currently approved")
        return {"proposal_id": str(proposal_id), "state": "REVOKED"}

    @router.post("/tool-capabilities", status_code=201)
    def issue_capability(
        request: CapabilityInput, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        try:
            return require_policy().issue(
                request.proposal_id, request.variant, request.schedule,
                request.max_calls, request.lifetime_seconds, request.plan_id,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/tool-capabilities/{capability_id}/revoke")
    def revoke_capability(
        capability_id: UUID, x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        if not require_policy().revoke_capability(capability_id):
            raise HTTPException(404, "No active capability with this ID")
        return {"capability_id": str(capability_id), "revoked": True}

    @router.get("/tool-audit")
    def tool_audit(x_operator_token: str | None = Header(default=None)):
        require_operator(x_operator_token)
        return {"events": require_policy().audit()}

    @router.post("/agent/plans", status_code=201)
    def propose_plan(request: PlanInput):
        try:
            plan = choose_plan(
                strategy=request.strategy, variant=request.variant, seed=request.seed
            )
            return require_policy().record_plan(request.proposal_id, plan)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/agent/tools/run")
    async def execute_tool(
        request: ToolInput, authorization: str | None = Header(default=None),
    ):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(403, "A scoped tool capability is required")
        secret = authorization.removeprefix("Bearer ")
        policy = require_policy()
        if len(secret) > 256 or not policy.authorize(
            secret, request.variant, request.schedule, request.plan_id
        ):
            raise HTTPException(403, "Tool action denied by policy")
        # This is the only allowed tool and only uses the shipped fixture.
        witness = await execute(request.variant, request.schedule)
        if not policy.record_outcome(request.plan_id, witness):
            raise HTTPException(409, "A validated plan receipt could not be stored")
        return witness

    @router.get("/agent/plans/{plan_id}")
    def get_plan(plan_id: UUID):
        result = require_policy().get_plan(plan_id)
        if not result:
            raise HTTPException(404, "Plan not found")
        return result

    @router.post("/agent/plans/reconcile")
    def reconcile_plans(x_operator_token: str | None = Header(default=None)):
        require_operator(x_operator_token)
        return {"reconciled": require_policy().reconcile_stalled_plans()}

    @router.post("/agent/evaluate")
    async def evaluate(
        request: EvaluationInput,
        x_operator_token: str | None = Header(default=None),
    ):
        require_operator(x_operator_token)
        require_policy()
        return await compare_strategies(request.trials)

    return router
