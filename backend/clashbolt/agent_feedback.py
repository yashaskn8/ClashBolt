"""Deterministic evidence-gap labels and bounded synthetic replan proposals.

These functions never grant tool access or turn ambiguous observations into a
finding. The known-inventory follow-up is intentionally small and explicit.
"""

from clashbolt.planner import propose_follow_up


def gap_class(reason: str) -> str:
    text = reason.lower()
    if "incompatible" in text or "schema" in text:
        return "SCHEMA_INCOMPATIBLE"
    if "receipt" in text or "deadline" in text or "missing" in text:
        return "MISSING_RECEIPT"
    if "oracle" in text or "revok" in text or "rule" in text:
        return "WITHDRAWN_ORACLE"
    if "incomplete" in text or "timeout" in text:
        return "INCOMPLETE_OBSERVATION"
    return "CONFLICTING_EVIDENCE"


def summarize_uncertainty(
    plan: dict, gaps: list[dict], feedback: dict | None = None
) -> dict:
    """Separate checker verdicts, abstention states, and missing confidence data."""
    result = plan.get("result") if isinstance(plan.get("result"), dict) else {}
    checker_verdict = result.get("verdict")
    plan_state = plan.get("state")
    quarantined = bool(result.get("quarantined"))
    if quarantined:
        uncertainty_state = "ABSTAINED_QUARANTINED"
    elif plan_state == "COMPLETE" and checker_verdict in ("PASS", "FAIL") and not gaps:
        uncertainty_state = "CERTIFIED"
    elif plan_state in ("PROPOSED", "IN_FLIGHT") and not gaps:
        uncertainty_state = "PENDING_OBSERVATION"
    else:
        uncertainty_state = "ABSTAINED_UNCERTAIN"
    return {
        "plan_state": plan_state,
        "checker_verdict": checker_verdict,
        "checker_verdict_authoritative": True,
        "uncertainty_state": uncertainty_state,
        "abstention_required": uncertainty_state != "CERTIFIED",
        "gap_count": len(gaps),
        "gap_classifications": sorted({item["classification"] for item in gaps}),
        "calibrated_confidence": None,
        "confidence_status": "UNAVAILABLE_WITHOUT_INDEPENDENT_ADJUDICATION",
        "probe_budget": {
            "max_steps": 32,
            "remaining_budget": feedback["remaining_budget"] if feedback else None,
            "automatically_authorized": False,
        },
    }


def decide_feedback(plan: dict, result: dict, budget_remaining: int) -> dict:
    """Choose at most one new *proposal*, never automatically dispatch it."""
    if type(budget_remaining) is not int or not 0 <= budget_remaining <= 32:
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
