"""Deterministic evidence-gap labels and bounded synthetic replan proposals.

These functions never grant tool access or turn ambiguous observations into a
finding. The known-inventory follow-up is intentionally small and explicit.
"""

from clashbolt.planner import propose_follow_up


def gap_class(reason: str) -> str:
    text = reason.lower()
    if "receipt" in text or "deadline" in text or "missing" in text:
        return "MISSING_RECEIPT"
    if "oracle" in text or "revok" in text or "rule" in text:
        return "WITHDRAWN_ORACLE"
    if "incomplete" in text or "timeout" in text:
        return "INCOMPLETE_OBSERVATION"
    return "CONFLICTING_EVIDENCE"


def decide_feedback(plan: dict, result: dict, budget_remaining: int) -> dict:
    """Choose at most one new *proposal*, never automatically dispatch it."""
    if not 0 <= budget_remaining <= 32:
        raise ValueError("Remaining gate budget must be between 0 and 32")
    previous = {
        "verdict": result.get("verdict", "UNKNOWN"),
        "variant": plan.get("variant"),
        "schedule": plan.get("schedule"),
    }
    decision = propose_follow_up(previous, budget_remaining=budget_remaining)
    return {
        **decision,
        "policy_version": "fixed-inventory-feedback-v1",
        "budget_remaining": budget_remaining,
        "next_step_cost": 4 if decision["action"] == "REPLAN" else 0,
        "automatically_authorized": False,
    }
