"""A deterministic, bounded baseline planner; no LLM advantage is asserted."""

import argparse
import asyncio
import json
import random
from hashlib import sha256
from typing import Literal

from clashbolt.evaluation_stats import paired_bootstrap
from clashbolt.experiment import (
    FIXTURE_HASH,
    ORACLE_HASH,
    TARGET_ID,
    TARGET_REVISION,
    execute,
)

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
    manifest = {
        "suite_id": "known-inventory-synthetic-v1",
        "target_revision": TARGET_REVISION,
        "fixture_sha256": FIXTURE_HASH,
        "oracle_sha256": ORACLE_HASH,
        "seeds": list(range(trials)),
        "gate_budget_per_trial": 4,
        "held_out": False,
    }
    manifest_sha256 = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    results = {}
    for strategy in ("semantic", "random", "coverage"):
        fails = 0
        unknowns = 0
        receipts = []
        for seed in range(trials):
            plan = choose_plan(strategy=strategy, variant="racy", seed=seed)
            witness = await execute(plan["variant"], plan["schedule"])
            fails += witness["verdict"] == "FAIL"
            unknowns += witness["verdict"] == "UNKNOWN"
            receipts.append({
                "seed": seed,
                "schedule": plan["schedule"],
                "verdict": witness["verdict"],
                "evidence_sha256": witness["evidence_sha256"],
            })
        results[strategy] = {
            "controlled_failures": fails,
            "unknowns": unknowns,
            "trials": trials,
            "gate_budget": 4 * trials,
            "receipts": receipts,
        }
    baseline = max(
        ("random", "coverage"),
        key=lambda name: results[name]["controlled_failures"],
    )
    paired = paired_bootstrap(
        [r["verdict"] == "FAIL" for r in results["semantic"]["receipts"]],
        [r["verdict"] == "FAIL" for r in results[baseline]["receipts"]],
    )
    return {
        "scope": "SYNTHETIC_TRAINING_FIXTURE",
        "paired_comparison": {
            "candidate": "semantic",
            "strongest_fixed_baseline": baseline,
            "statistics": paired,
            "warning": "Repeated seeds on one known fixture are not held-out evidence",
        },
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "claim": "deterministic baseline comparison; no AI or held-out validation",
        "promotion_eligible": False,
        "promotion_reason": (
            "No held-out applications, AI candidate, independent reproduction "
            "or measured equal-cost advantage"
        ),
        "strategies": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproduce local synthetic baseline trials")
    parser.add_argument("--trials", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.trials <= 20:
        parser.error("Trials must be 1-20")
    comparison = asyncio.run(compare_strategies(args.trials))
    print(json.dumps(comparison, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
