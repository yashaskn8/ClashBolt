"""Descriptive paired-bootstrap uncertainty on *already adjudicated* outcomes.

This only calculates an interval; it never approves a model or validates
untrusted evidence. Trials on the same synthetic target are NOT independent
held-out applications, regardless of apparent confidence interval width.
"""

import random


def paired_bootstrap(
    candidate: list[bool], baseline: list[bool], seed: int = 42,
) -> dict:
    if (
        not candidate
        or len(candidate) != len(baseline)
        or len(candidate) > 1_000
        or type(seed) is not int
        or not 0 <= seed <= 1_000_000
        or any(type(value) is not bool for value in candidate + baseline)
    ):
        raise ValueError("Paired outcome vectors must have equal bounded boolean entries")
    changes = [int(a) - int(b) for a, b in zip(candidate, baseline, strict=True)]
    count = len(changes)
    estimate = sum(changes) / count
    rng = random.Random(seed)
    resamples = sorted(
        sum(changes[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(1_000)
    )
    return {
        "observations": count,
        "paired_mean_difference": round(estimate, 6),
        "descriptive_bootstrap_95": [
            round(resamples[24], 6),
            round(resamples[974], 6),
        ],
        "bootstrap_resamples": 1_000,
        "independent_targets_proven": False,
        "promotion_eligible": False,
    }
