"""Ungated two-client measurements on the one shipped native PostgreSQL fixture.

This is a trusted local-only research command: it never connects to external
targets or runs model-supplied code. No checkpoint release order is imposed;
observations from different threads are *not* a reconstructed causal trace.
A controlled witness and an ungated sample have different evidence contracts.
"""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from uuid import uuid4

from clashbolt.experiment import (
    FIXTURE_HASH,
    TARGET_ID,
    TARGET_REVISION,
    PostgresInventoryFixture,
    check_invariant,
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
            sample = sample_ungated(target.url, variant)
        samples.append({
            "trial": index + 1,
            "elapsed_wall_seconds": round(perf_counter() - start, 6),
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure ungated scheduling of the one native synthetic fixture"
    )
    parser.add_argument("--variant", choices=("racy", "locked"), default="racy")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_DATABASE_URL")
    if not admin or not control:
        parser.error("Set separate local target-admin and control database URLs")
    try:
        report = measure_ungated(
            admin, control, variant=args.variant, trials=args.trials
        )
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
