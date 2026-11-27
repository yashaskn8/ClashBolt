import pytest
from clashbolt.agent_feedback import decide_feedback, gap_class, summarize_uncertainty


def test_evidence_gaps_are_labeled_without_promoting_unknowns():
    assert gap_class("Incompatible decision schema") == "SCHEMA_INCOMPATIBLE"
    assert gap_class("Tool receipt missing after deadline") == "MISSING_RECEIPT"
    assert gap_class("oracle approval revoked") == "WITHDRAWN_ORACLE"
    assert gap_class("Incomplete target observation") == "INCOMPLETE_OBSERVATION"
    assert gap_class("Changed checksum") == "CONFLICTING_EVIDENCE"


def test_summarize_uncertainty_separates_verdict_abstention_and_confidence():
    certified = summarize_uncertainty(
        {"state": "COMPLETE", "result": {"verdict": "FAIL"}}, []
    )
    assert certified["checker_verdict"] == "FAIL"
    assert certified["uncertainty_state"] == "CERTIFIED"
    assert certified["abstention_required"] is False
    assert certified["calibrated_confidence"] is None

    pending = summarize_uncertainty({"state": "PROPOSED", "result": None}, [])
    assert pending["uncertainty_state"] == "PENDING_OBSERVATION"
    assert pending["abstention_required"] is True

    uncertain = summarize_uncertainty(
        {"state": "UNKNOWN", "result": {"verdict": "UNKNOWN"}},
        [{"classification": "MISSING_RECEIPT"}],
        feedback={"remaining_budget": 4},
    )
    assert uncertain["uncertainty_state"] == "ABSTAINED_UNCERTAIN"
    assert uncertain["gap_classifications"] == ["MISSING_RECEIPT"]
    assert uncertain["probe_budget"]["remaining_budget"] == 4

    quarantined = summarize_uncertainty(
        {"state": "UNKNOWN", "result": {"verdict": "UNKNOWN", "quarantined": True}},
        [{"classification": "SCHEMA_INCOMPATIBLE"}],
    )
    assert quarantined["uncertainty_state"] == "ABSTAINED_QUARANTINED"
    assert quarantined["abstention_required"] is True


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


@pytest.mark.parametrize("verdict", [None, "", "UNRECOGNIZED", True, ["PASS"]])
def test_malformed_verdict_cannot_authorize_a_follow_up_proposal(verdict):
    decision = decide_feedback({"variant": "racy", "schedule": "serial"}, {"verdict": verdict}, 4)
    assert decision["action"] == "ABSTAIN"
    assert decision["next_step_cost"] == 0


@pytest.mark.parametrize("budget", [True, 4.0, -1])
def test_feedback_requires_a_bounded_integer_budget(budget):
    with pytest.raises(ValueError, match="Remaining gate budget"):
        decide_feedback({"variant": "racy"}, {"verdict": "PASS"}, budget)


@pytest.mark.parametrize("variant,schedule", [("locked", "serial"), ("racy", "overlap")])
def test_feedback_does_not_repeat_a_test_without_an_untried_follow_up(variant, schedule):
    result = decide_feedback({"variant": variant, "schedule": schedule}, {"verdict": "PASS"}, 8)
    assert result["action"] == "ABSTAIN"
