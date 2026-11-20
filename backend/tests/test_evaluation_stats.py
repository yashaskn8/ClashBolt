"""Descriptive statistics cannot turn known synthetic training into AI evidence."""

import asyncio

import pytest
from clashbolt.evaluation_stats import paired_bootstrap
from clashbolt.planner import compare_strategies


def test_paired_interval_is_seeded_and_zero_when_outcomes_match():
    identical = paired_bootstrap(
        [True, False, True, True], [True, False, True, True]
    )
    assert identical["paired_mean_difference"] == 0
    assert identical["descriptive_bootstrap_95"] == [0, 0]
    assert identical == paired_bootstrap(
        [True, False, True, True], [True, False, True, True]
    )
    assert identical["promotion_eligible"] is False


def test_paired_interval_rejects_misaligned_and_nonboolean_input():
    with pytest.raises(ValueError):
        paired_bootstrap([True], [])
    with pytest.raises(ValueError):
        paired_bootstrap([True, 1], [False, True])
    with pytest.raises(ValueError):
        paired_bootstrap([True], [False], seed=-1)


def test_synthetic_baseline_includes_uncertainty_without_promotion():
    evaluated = asyncio.run(compare_strategies(8))
    paired = evaluated["paired_comparison"]
    assert paired["candidate"] == "semantic"
    assert paired["strongest_fixed_baseline"] in ("random", "coverage")
    assert paired["statistics"]["observations"] == 8
    assert paired["statistics"]["promotion_eligible"] is False
    assert evaluated["promotion_eligible"] is False
    assert evaluated["manifest"]["held_out"] is False
