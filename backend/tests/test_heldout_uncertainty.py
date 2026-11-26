"""Incomplete reported comparisons must not imply known negative outcomes."""

import json
from itertools import product

import pytest
from clashbolt.heldout import PROTOCOL, STRATEGIES, canonical_hash, compare_reported_observations
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule


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


class ReportedOutcomeTransitions(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.outcomes = ([[None, None], [None, None]], [[None, None], [None, None]])
        self.report = report_for(*self.outcomes)

    @rule(
        candidate=st.booleans(),
        target_index=st.integers(min_value=0, max_value=1),
        seed_index=st.integers(min_value=0, max_value=1),
        found=st.booleans(),
    )
    def resolve_trial(self, candidate, target_index, seed_index, found):
        trials = self.outcomes[0 if candidate else 1][target_index]
        if trials[seed_index] is None:
            trials[seed_index] = found

        next_report = report_for(*self.outcomes)
        old_results = self.report["paired_outcome_uncertainty"]
        for strategy, result in next_report["paired_outcome_uncertainty"].items():
            old = old_results[strategy]
            old_lower, old_upper = old["possible_mean_difference"]
            lower, upper = result["possible_mean_difference"]
            assert old_lower <= lower <= upper <= old_upper
            assert result["resolved_pairs"] >= old["resolved_pairs"]
        self.report = next_report

    @invariant()
    def reports_preserve_outcome_counts_and_are_json_safe(self):
        for strategy, totals in self.report["strategies"].items():
            targets = self.outcomes[0 if strategy == "semantic" else 1]
            assert totals["reported_positive_targets"] == sum(True in row for row in targets)
            assert totals["reported_negative_targets"] == sum(
                row == [False, False] for row in targets
            )
            assert totals["unresolved_targets"] == sum(
                True not in row and None in row for row in targets
            )

        assert json.loads(json.dumps(self.report, allow_nan=False)) == self.report
        assert self.report["promotion_eligible"] is False


TestReportedOutcomeTransitions = ReportedOutcomeTransitions.TestCase
TestReportedOutcomeTransitions.settings = settings(max_examples=25, stateful_step_count=12)
