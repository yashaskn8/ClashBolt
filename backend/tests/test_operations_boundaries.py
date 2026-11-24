"""Local diagnostics identify ambiguous outcomes without taking action."""

import pytest
from clashbolt.metrics import operational_metrics
from clashbolt.ops_alerts import diagnose_operations


@pytest.mark.parametrize("runs, verifications, expected", [
    ({}, {}, []),
    ({"UNKNOWN": 2}, {}, ["UNKNOWN_RUN_OUTCOMES"]),
    ({}, {"UNKNOWN": 1}, ["UNKNOWN_VERIFICATION_OUTCOMES"]),
    ({"UNKNOWN": 1}, {"UNKNOWN": 1}, [
        "UNKNOWN_RUN_OUTCOMES", "UNKNOWN_VERIFICATION_OUTCOMES",
    ]),
    ({"COMPLETE": 2, "CANCELLED": 1}, {"COMPLETE": 2}, []),
])
def test_ambiguous_outcomes_require_operator_review(runs, verifications, expected):
    snapshot = {
        "runs": runs, "verifications": verifications, "responsive_workers": 1,
    }
    result = diagnose_operations(snapshot)
    assert [alert["code"] for alert in result["alerts"]] == expected
    assert result["status"] == ("ATTENTION" if expected else "NORMAL")
    assert result["notifications_sent"] is False
    assert result["automatic_remediation"] is False


@pytest.mark.parametrize("age, workers, expected", [
    (119.999, 1, []),
    (120, 1, ["QUEUE_WAIT_OVER_TWO_MINUTES"]),
    (0, 0, ["NO_RESPONSIVE_WORKER", "VERIFICATION_NEEDS_WORKER"]),
])
def test_queue_boundaries(age, workers, expected):
    result = diagnose_operations({
        "runs": {"PENDING": 1}, "verifications": {"PENDING": 1},
        "oldest_pending_seconds": age, "responsive_workers": workers,
    })
    assert [alert["code"] for alert in result["alerts"]] == expected


def test_quarantine_is_independent_of_worker_availability():
    result = diagnose_operations({
        "runs": {}, "verifications": {}, "responsive_workers": 1,
        "quarantined_agent_plans": 2,
    })
    assert result["status"] == "ATTENTION"
    assert [alert["code"] for alert in result["alerts"]] == ["QUARANTINED_AGENT_CONTEXT"]


@pytest.mark.parametrize("age, latency, expected_age, expected_latency", [
    (-3, -1, "0.000", "0.000"),
    (120.125, 0.375, "120.125", "0.375"),
])
def test_metrics_preserve_bounded_operational_gauges(age, latency, expected_age, expected_latency):
    output = operational_metrics({
        "runs": {"UNKNOWN": 2}, "verifications": {"UNKNOWN": 3},
        "responsive_workers": 1, "workers": [{"owner": "private-worker-name"}],
        "native_slots_occupied": 2, "native_slots_limit": 4,
        "oldest_pending_seconds": age, "start_p95_seconds": latency,
    })
    samples = {line for line in output.splitlines() if not line.startswith("#")}
    assert 'clashbolt_runs{state="UNKNOWN"} 2' in samples
    assert 'clashbolt_verification_jobs{state="UNKNOWN"} 3' in samples
    assert 'clashbolt_runs{state="COMPLETE"} 0' in samples
    assert "clashbolt_native_slots_occupied 2" in samples
    assert "clashbolt_native_slots_limit 4" in samples
    assert f"clashbolt_oldest_pending_seconds {expected_age}" in samples
    assert f"clashbolt_run_start_p95_seconds {expected_latency}" in samples
    assert "private-worker-name" not in output
