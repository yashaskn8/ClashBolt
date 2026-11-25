"""Active work without a responsive fleet needs review, not a verdict."""

from copy import deepcopy

import pytest
from clashbolt.ops_alerts import diagnose_operations


@pytest.mark.parametrize("runs, jobs, workers, codes", [
    ({"RUNNING": 1}, {}, 0, ["ACTIVE_RUN_NEEDS_REVIEW"]),
    ({"CANCELLING": 1}, {}, 0, ["ACTIVE_RUN_NEEDS_REVIEW"]),
    ({}, {"RUNNING": 1}, 0, ["ACTIVE_VERIFICATION_NEEDS_REVIEW"]),
    ({"RUNNING": 1, "CANCELLING": 2}, {"RUNNING": 2}, 0,
     ["ACTIVE_RUN_NEEDS_REVIEW", "ACTIVE_VERIFICATION_NEEDS_REVIEW"]),
    ({"RUNNING": 1, "CANCELLING": 1}, {"RUNNING": 1}, 1, []),
    ({"COMPLETE": 1, "CANCELLED": 1}, {"COMPLETE": 1}, 0, []),
    ({"PENDING": 1, "RUNNING": 1}, {"PENDING": 1, "RUNNING": 1}, 0,
     ["NO_RESPONSIVE_WORKER", "VERIFICATION_NEEDS_WORKER",
      "ACTIVE_RUN_NEEDS_REVIEW", "ACTIVE_VERIFICATION_NEEDS_REVIEW"]),
    ({"RUNNING": 1, "UNKNOWN": 1}, {"UNKNOWN": 1}, 0,
     ["ACTIVE_RUN_NEEDS_REVIEW", "UNKNOWN_RUN_OUTCOMES",
      "UNKNOWN_VERIFICATION_OUTCOMES"]),
])
def test_active_work_without_heartbeat(runs, jobs, workers, codes):
    snapshot = {"runs": runs, "verifications": jobs, "responsive_workers": workers}
    before = deepcopy(snapshot)
    result = diagnose_operations(snapshot)
    assert [alert["code"] for alert in result["alerts"]] == codes
    assert result["status"] == ("ATTENTION" if codes else "NORMAL")
    assert result["notifications_sent"] is False
    assert result["automatic_remediation"] is False
    assert snapshot == before


def test_review_hint_clears_when_fleet_heartbeat_recovers():
    snapshot = {
        "runs": {"CANCELLING": 1}, "verifications": {"RUNNING": 1},
        "responsive_workers": 0,
    }
    assert diagnose_operations(snapshot)["status"] == "ATTENTION"
    snapshot["responsive_workers"] = 1
    assert diagnose_operations(snapshot)["status"] == "NORMAL"
    assert snapshot["runs"]["CANCELLING"] == 1
    assert snapshot["verifications"]["RUNNING"] == 1


@pytest.mark.parametrize("age, quarantine, workers, codes", [
    (119.999, 0, 1, []),
    (120, 2, 1, ["QUEUE_WAIT_OVER_TWO_MINUTES", "QUARANTINED_AGENT_CONTEXT"]),
    (0, 0, 0, ["NO_RESPONSIVE_WORKER", "VERIFICATION_NEEDS_WORKER"]),
])
def test_pending_review_hints_remain_compatible(age, quarantine, workers, codes):
    snapshot = {
        "runs": {"PENDING": 1}, "verifications": {"PENDING": 1},
        "responsive_workers": workers, "oldest_pending_seconds": age,
        "quarantined_agent_plans": quarantine,
    }
    assert [alert["code"] for alert in diagnose_operations(snapshot)["alerts"]] == codes
