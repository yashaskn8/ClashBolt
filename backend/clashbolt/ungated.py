"""Ungated two-client measurements on the one shipped native PostgreSQL fixture.

This is a trusted local-only research command: it never connects to external
targets or runs model-supplied code. No checkpoint release order is imposed;
observations from different threads are *not* a reconstructed causal trace.
A controlled witness and an ungated sample have different evidence contracts.
"""

import argparse
import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from statistics import median
from time import perf_counter
from uuid import uuid4

from clashbolt.experiment import (
    FIXTURE_HASH,
    SCHEDULES,
    TARGET_ID,
    TARGET_REVISION,
    PostgresInventoryFixture,
    check_invariant,
    execute_postgres,
)
from clashbolt.target_db import disposable_target


def sample_ungated(target_dsn: str, variant: str) -> dict:
    """Use two competing native database clients without per-operation gates."""
    if variant not in ("racy", "locked"):
        raise ValueError("Only the shipped fixed inventory variants are supported")
    fixture = PostgresInventoryFixture(target_dsn)

    def client(identity: str) -> dict:
        read = fixture.read(identity)
        commit = fixture.commit(identity, read["observed_stock"], variant)
        return {"client": identity, "read": read, "commit": commit}

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(client, "a")
        b = executor.submit(client, "b")
        # Each fixed client owns its read and commit; do not infer a global
        # interleaving or controlled gate sequence from these grouped results.
        observed = [a.result(timeout=10), b.result(timeout=10)]
    state = fixture.final_state()
    return {
        "mode": "UNGATED_NATIVE_SAMPLE",
        "target_id": TARGET_ID,
        "target_revision": TARGET_REVISION,
        "fixture_sha256": FIXTURE_HASH,
        "variant": variant,
        "client_observations": observed,
        "final_state": state,
        "verdict": check_invariant(state, complete=True),
        "independent_target_proven": False,
        "controlled_schedule": False,
        "scope": "SHIPPED_SYNTHETIC_FIXTURE_ONLY",
    }


def measure_ungated(
    target_admin_url: str, control_url: str, *, variant: str, trials: int,
) -> dict:
    if variant not in ("racy", "locked") or type(trials) is not int or not 1 <= trials <= 20:
        raise ValueError("Only 1-20 trials of a known synthetic variant are allowed")
    samples = []
    for index in range(trials):
        # This trusted admin CLI creates a separate disposable database and
        # restricted login for *every* trial; no state can carry between them.
        start = perf_counter()
        with disposable_target(target_admin_url, control_url, uuid4()) as target:
            exec_start = perf_counter()
            sample = sample_ungated(target.url, variant)
            exec_ms = round((perf_counter() - exec_start) * 1000, 3)
            provision_ms = target.provision_ms
        samples.append({
            "trial": index + 1,
            "elapsed_wall_seconds": round(perf_counter() - start, 6),
            "execution_wall_ms": exec_ms,
            "provision_ms": provision_ms,
            **sample,
        })
    return {
        "scope": "SHIPPED_SYNTHETIC_FIXTURE_ONLY",
        "benchmark": "UNGATED_NATIVE_TWO_CLIENTS",
        "variant": variant,
        "trials": trials,
        "pass_count": sum(s["verdict"] == "PASS" for s in samples),
        "fail_count": sum(s["verdict"] == "FAIL" for s in samples),
        "unknown_count": sum(s["verdict"] == "UNKNOWN" for s in samples),
        "samples": samples,
        "reproduction_claim": "OBSERVED_ONLY_NOT_GUARANTEED",
        "general_target_supported": False,
    }


def measure_gate_overhead(
    target_admin_url: str,
    control_url: str,
    *,
    variant: str,
    schedule: str = "overlap",
    trials: int = 3,
) -> dict:
    """Measure controlled-schedule gate overhead vs ungated execution on fresh targets."""
    if (
        variant not in ("racy", "locked")
        or schedule not in SCHEDULES
        or type(trials) is not int
        or not 1 <= trials <= 20
    ):
        raise ValueError("Invalid variant, schedule, or bounded trial count (1-20)")
    controlled_times: list[float] = []
    ungated_times: list[float] = []
    provision_times: list[float] = []
    controlled_verdicts: list[str] = []
    ungated_verdicts: list[str] = []
    for _ in range(trials):
        with disposable_target(target_admin_url, control_url, uuid4()) as target:
            if target.provision_ms is not None:
                provision_times.append(target.provision_ms)
            t0 = perf_counter()
            witness = asyncio.run(execute_postgres(variant, schedule, target.url))
            controlled_times.append(round((perf_counter() - t0) * 1000, 3))
            controlled_verdicts.append(witness["verdict"])
        with disposable_target(target_admin_url, control_url, uuid4()) as target:
            if target.provision_ms is not None:
                provision_times.append(target.provision_ms)
            t0 = perf_counter()
            sample = sample_ungated(target.url, variant)
            ungated_times.append(round((perf_counter() - t0) * 1000, 3))
            ungated_verdicts.append(sample["verdict"])
    controlled_med = round(median(controlled_times), 3)
    ungated_med = round(median(ungated_times), 3)
    return {
        "scope": "SHIPPED_SYNTHETIC_FIXTURE_ONLY",
        "benchmark": "CONTROLLED_GATE_OVERHEAD",
        "variant": variant,
        "schedule": schedule,
        "trials": trials,
        "controlled_median_ms": controlled_med,
        "ungated_median_ms": ungated_med,
        "gate_overhead_median_ms": round(controlled_med - ungated_med, 3),
        "target_provision_median_ms": (
            round(median(provision_times), 3) if provision_times else None
        ),
        "controlled_fail_count": sum(v == "FAIL" for v in controlled_verdicts),
        "controlled_pass_count": sum(v == "PASS" for v in controlled_verdicts),
        "ungated_fail_count": sum(v == "FAIL" for v in ungated_verdicts),
        "ungated_pass_count": sum(v == "PASS" for v in ungated_verdicts),
        "general_target_supported": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure ungated scheduling of the one native synthetic fixture"
    )
    parser.add_argument("--variant", choices=("racy", "locked"), default="racy")
    parser.add_argument("--schedule", choices=("overlap", "serial"), default="overlap")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--compare-controlled", action="store_true")
    args = parser.parse_args()
    admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_DATABASE_URL")
    if not admin or not control:
        parser.error("Set separate local target-admin and control database URLs")
    try:
        if args.compare_controlled:
            report = measure_gate_overhead(
                admin, control, variant=args.variant,
                schedule=args.schedule, trials=args.trials,
            )
        else:
            report = measure_ungated(
                admin, control, variant=args.variant, trials=args.trials
            )
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
