"""A deterministic, bounded baseline planner; no LLM advantage is asserted."""

import random
from typing import Literal

from clashbolt.experiment import TARGET_ID, TARGET_REVISION, execute

Strategy = Literal["semantic", "random", "coverage"]
Variant = Literal["racy", "locked"]


def choose_plan(
    *, strategy: Strategy, variant: Variant, seed: int = 0, prior_covered: list[str] | None = None
) -> dict:
    if strategy not in ("semantic", "random", "coverage"):
        raise ValueError("Unknown deterministic plan strategy")
    if variant not in ("racy", "locked") or not 0 <= seed <= 1_000_000:
        raise ValueError("Invalid fixture or seed")

    if strategy == "semantic":
        schedule = "overlap"  # Conflict on the same inventory key.
        reason = "Both clients touch shared stock; investigate overlapping reads"
    elif strategy == "random":
        schedule = random.Random(seed).choice(["overlap", "serial"])
        reason = "Seeded random baseline"
    else:
        covered = prior_covered or []
        schedule = "serial" if "overlap" in covered and "serial" not in covered else "overlap"
        reason = "Explore an uncovered application gate order"
    return {
        "strategy": strategy,
        "seed": seed,
        "target_id": TARGET_ID,
        "target_revision": TARGET_REVISION,
        "variant": variant,
        "schedule": schedule,
        "conflict_edge": "shared.inventory",
        "max_steps": 4,
        "stop_after": 1,
        "reason": reason,
        "planner_kind": "DETERMINISTIC_BASELINE",
    }


def propose_follow_up(previous: dict, *, budget_remaining: int) -> dict:
    """Never reinterpret UNKNOWN or expand budget while replanning."""
    if budget_remaining < 4 or previous.get("verdict") == "UNKNOWN":
        return {"action": "ABSTAIN", "reason": "Insufficient budget or evidence"}
    if previous.get("verdict") == "FAIL":
        return {"action": "STOP", "reason": "A controlled failing witness exists"}
    if previous.get("variant") != "racy":
        return {"action": "ABSTAIN", "reason": "No untried synthetic conflict selected"}
    if previous.get("schedule") == "serial":
        return {"action": "REPLAN", "plan": choose_plan(strategy="semantic", variant="racy")}
    return {"action": "ABSTAIN", "reason": "No untried synthetic conflict selected"}


async def compare_strategies(trials: int = 8) -> dict:
    """Equal gate budgets on the shipped fixture, not held-out AI evaluation."""
    if not 1 <= trials <= 20:
        raise ValueError("Trials must be 1-20")
    results = {}
    for strategy in ("semantic", "random", "coverage"):
        fails = 0
        unknowns = 0
        for seed in range(trials):
            plan = choose_plan(strategy=strategy, variant="racy", seed=seed)
            witness = await execute(plan["variant"], plan["schedule"])
            fails += witness["verdict"] == "FAIL"
            unknowns += witness["verdict"] == "UNKNOWN"
        results[strategy] = {
            "controlled_failures": fails,
            "unknowns": unknowns,
            "trials": trials,
            "gate_budget": 4 * trials,
        }
    return {
        "scope": "SYNTHETIC_TRAINING_FIXTURE",
        "claim": "deterministic baseline comparison; no AI or held-out validation",
        "strategies": results,
    }
