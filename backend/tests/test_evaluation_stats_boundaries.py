"""Exercise bounded paired statistics without invoking a target or model."""

import pytest
from clashbolt.evaluation_stats import paired_bootstrap


@pytest.mark.parametrize("candidate, baseline, seed", [
    ([], [], 42),
    ([True], [], 42),
    ([True] * 1_001, [False] * 1_001, 42),
    ([True], [False], True),
    ([True], [False], -1),
    ([True], [False], 1_000_001),
    ([1], [False], 42),
    ([True], [0], 42),
])
def test_invalid_paired_inputs(candidate, baseline, seed):
    with pytest.raises(ValueError, match="equal bounded boolean entries"):
        paired_bootstrap(candidate, baseline, seed)


@pytest.mark.parametrize("candidate, baseline, seed, expected", [
    ([True], [False], 0, 1),
    ([False], [True], 1_000_000, -1),
    ([False], [False], 42, 0),
])
def test_constant_paired_differences(candidate, baseline, seed, expected):
    result = paired_bootstrap(candidate, baseline, seed)
    assert result["observations"] == 1
    assert result["paired_mean_difference"] == expected
    assert result["descriptive_bootstrap_95"] == [expected, expected]
    assert result["bootstrap_resamples"] == 1_000
    assert result["independent_targets_proven"] is False
    assert result["promotion_eligible"] is False
