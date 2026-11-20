"""Evidence-bound, deterministic frontier over the *two shipped* gate schedules.

This is a functional scoring mechanism, not a benchmark-backed AI advantage.
All observations must come from independently validated, stored plan receipts.
"""

from typing import Literal

from clashbolt.experiment import SCHEDULES, TARGET_REVISION
from clashbolt.planner import choose_plan


def rank_frontier(receipts: list[dict], variant: Literal["racy", "locked"]) -> list[dict]:
    if variant not in ("racy", "locked"):
        raise ValueError("Unknown fixture variant")
    counts = {
        name: {"attempts": 0, "failures": 0, "unknowns": 0}
        for name in SCHEDULES
    }
    for item in receipts:
        plan = item.get("plan") or {}
        result = item.get("result") or {}
        schedule = plan.get("schedule")
        if (
            plan.get("variant") != variant
            or plan.get("target_revision") != TARGET_REVISION
            or schedule not in counts
        ):
            continue
        verdict = result.get("verdict")
        if verdict not in ("PASS", "FAIL", "UNKNOWN"):
            continue
        counts[schedule]["attempts"] += 1
        counts[schedule]["failures"] += verdict == "FAIL"
        counts[schedule]["unknowns"] += verdict == "UNKNOWN"

    candidates = []
    for schedule, observed in counts.items():
        attempts = observed["attempts"]
        failures = observed["failures"]
        # Beta(1,1) smoothing prevents certainty from small samples.
        expected_failure = (failures + 1) / (attempts + 2)
        # An explicit novelty discount keeps the search exploring.
        cost_adjusted = expected_failure / (1 + attempts / 4)
        candidates.append({
            "edge": "shared.inventory",
            "schedule": schedule,
            "attempts": attempts,
            "confirmed_failures": failures,
            "unknowns": observed["unknowns"],
            "estimated_failure_probability": round(expected_failure, 6),
            "priority": round(cost_adjusted, 6),
            "gate_cost": 4,
        })
    # Same-score ties are deterministic; overlap is the known semantic conflict.
    return sorted(
        candidates, key=lambda item: (
            -item["priority"], item["schedule"] != "overlap"
        ),
    )


def next_frontier_plan(
    receipts: list[dict], variant: Literal["racy", "locked"],
) -> dict:
    ranked = rank_frontier(receipts, variant)
    selected = ranked[0]
    plan = choose_plan(strategy="semantic", variant=variant)
    return {
        **plan,
        "schedule": selected["schedule"],
        "planner_kind": "DETERMINISTIC_EVIDENCE_FRONTIER",
        "frontier_policy": "known-inventory-frontier-v1",
        "estimated_information_gain": selected["priority"],
        "reason": "Select bounded shared-inventory gate order using verified receipts",
    }
