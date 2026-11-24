"""Incomplete reported comparisons must not imply known negative outcomes."""

from itertools import product

import pytest
from clashbolt.heldout import PROTOCOL, STRATEGIES, canonical_hash, compare_reported_observations


def report_for(candidate, baseline):
    manifest = {
        "protocol": PROTOCOL, "suite_id": "test-missingness",
        "training_targets": [], "heldout_targets": ["target-a", "target-b"],
        "seeds": [0, 1], "strategies": list(STRATEGIES),
        "cpu_seconds_per_trial": 10, "model_tokens_per_trial": 10,
    }
    rows = []
    for i, target in enumerate(manifest["heldout_targets"]):
        for seed, strategy in product(manifest["seeds"], STRATEGIES):
            values = candidate if strategy == "semantic" else baseline
            outcome = values[i][seed]
            rows.append({
                "target_id": target, "seed": seed, "strategy": strategy,
                "status": "UNKNOWN" if outcome is None else "COMPLETE",
                "bug_ids": ["reported-1"] if outcome else [],
                "cpu_seconds": 1, "model_tokens": 0,
                "evidence_sha256": "a" * 64 if outcome else None,
            })
    return compare_reported_observations(manifest, {
        "manifest_sha256": canonical_hash(manifest), "observations": rows,
    })


@pytest.mark.parametrize("candidate, baseline, bounds, resolved", [
    (((True, False), (False, False)), ((False, False), (False, False)), [0.5, 0.5], 2),
    (((None, False), (False, False)), ((False, False), (False, False)), [0, 0.5], 1),
    (((False, False), (False, False)), ((None, False), (False, False)), [-0.5, 0], 1),
    (((None, None), (None, None)), ((None, None), (None, None)), [-1, 1], 0),
    (((True, None), (False, False)), ((False, False), (False, False)), [0.5, 0.5], 2),
    (((True, False), (None, False)), ((None, False), (True, False)), [-0.5, 0.5], 0),
])
def test_unknown_outcome_bounds(candidate, baseline, bounds, resolved):
    report = report_for(candidate, baseline)
    comparisons = report["paired_outcome_uncertainty"]
    assert set(comparisons) == set(STRATEGIES[1:])
    for result in comparisons.values():
        assert result["possible_mean_difference"] == bounds
        assert result["resolved_pairs"] == resolved
        assert result["unresolved_pairs"] == 2 - resolved
    assert report["promotion_eligible"] is False
    assert report["paired_descriptive_interval_basis"] == "reported_findings_only"


def test_all_unknown_is_not_a_known_zero_effect():
    unknown = ((None, None), (None, None))
    report = report_for(unknown, unknown)
    assert report["strategies"]["semantic"]["unresolved_targets"] == 2
    assert report["strategies"]["semantic"]["reported_positive_targets"] == 0
    assert report["strategies"]["semantic"]["reported_negative_targets"] == 0
    assert report["paired_outcome_uncertainty"]["random"]["possible_mean_difference"] == [-1, 1]
