"""Optional local model suggestions. The planner never receives tool authority.

A model chooses from two *shipped* synthetic schedules. The backend constructs
the authoritative target/revision, budget, and stop rule; model fields cannot
increase privileges, add operations, or approve an oracle.
"""

import hashlib
import json
import os
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clashbolt.experiment import ORACLE_HASH, TARGET_REVISION
from clashbolt.planner import choose_plan

MODEL_URL = "http://127.0.0.1:11434/api/generate"
PROMPT_VERSION = "local-inventory-plan-v1"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schedule: Literal["overlap", "serial"]
    conflict_edge: Literal["shared.inventory"]
    reason: str = Field(min_length=8, max_length=240)
    information_gain: float = Field(ge=0, le=1)


async def propose_model_plan(
    variant: Literal["racy", "locked"], transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    """One bounded local request; fail closed to a labeled baseline on errors."""
    baseline = choose_plan(strategy="semantic", variant=variant)
    model = os.getenv("CLASHBOLT_LOCAL_MODEL", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", model):
        return {
            **baseline,
            "planner_kind": "DETERMINISTIC_FALLBACK",
            "model_status": "NOT_CONFIGURED",
        }

    instructions = {
        "contract": PROMPT_VERSION,
        "target_revision": TARGET_REVISION,
        "oracle_sha256": ORACLE_HASH,
        "fixture_variant": variant,
        "possible_schedules": ["overlap", "serial"],
        "known_operations": ["a.read", "b.read", "a.commit", "b.commit"],
        "task": (
            "Suggest one schedule for testing a shared inventory invariant. "
            "Return only JSON with schedule, conflict_edge='shared.inventory', "
            "reason and information_gain. No tools or other target IDs."
        ),
    }
    payload = {
        "model": model,
        "prompt": json.dumps(instructions, sort_keys=True),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 256},
    }
    try:
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False, transport=transport
        ) as client:
            response = await client.post(MODEL_URL, json=payload)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            raise ValueError("Local model response is missing a JSON string")
        if len(body["response"]) > 4096:
            raise ValueError("Local model output exceeds the fixed size limit")
        token_count = body.get("eval_count", 0)
        if type(token_count) is not int or not 0 <= token_count <= 256:
            raise ValueError("Model output token usage exceeded budget")
        suggestion = Candidate.model_validate_json(body["response"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError):
        return {
            **baseline,
            "planner_kind": "DETERMINISTIC_FALLBACK",
            "model_status": "UNAVAILABLE_OR_INVALID",
        }

    return {
        **baseline,
        "strategy": "model",
        "schedule": suggestion.schedule,
        "conflict_edge": suggestion.conflict_edge,
        "reason": suggestion.reason,
        "expected_information_gain": suggestion.information_gain,
        "planner_kind": "LOCAL_MODEL_UNPROMOTED",
        "model_id": model,
        "model_policy_sha256": hashlib.sha256(PROMPT_VERSION.encode()).hexdigest(),
        "model_tokens_used": token_count,
        "model_token_budget": 256,
        "model_status": "VALIDATED_PROPOSAL",
    }
