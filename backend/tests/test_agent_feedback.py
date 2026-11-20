import pytest

from clashbolt.agent_feedback import decide_feedback, gap_class


def test_evidence_gaps_are_labeled_without_promoting_unknowns():
    assert gap_class("Tool receipt missing after deadline") == "MISSING_RECEIPT"
    assert gap_class("oracle approval revoked") == "WITHDRAWN_ORACLE"
    assert gap_class("Incomplete target observation") == "INCOMPLETE_OBSERVATION"
    assert gap_class("Changed checksum") == "CONFLICTING_EVIDENCE"


def test_feedback_stops_or_replans_only_inside_fixed_gate_budget():
    previous = {"variant": "racy", "schedule": "serial"}
    result = decide_feedback(previous, {"verdict": "PASS"}, 4)
    assert result["action"] == "REPLAN"
    assert result["plan"]["schedule"] == "overlap"
    assert result["next_step_cost"] == 4
    assert result["automatically_authorized"] is False
    assert decide_feedback(previous, {"verdict": "FAIL"}, 8)["action"] == "STOP"
    assert decide_feedback(previous, {"verdict": "UNKNOWN"}, 8)["action"] == "ABSTAIN"
    assert decide_feedback(previous, {"verdict": "PASS"}, 3)["action"] == "ABSTAIN"
    with pytest.raises(ValueError):
        decide_feedback(previous, {"verdict": "PASS"}, 33)
