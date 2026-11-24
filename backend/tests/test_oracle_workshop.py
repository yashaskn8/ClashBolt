import pytest
from clashbolt.oracle_workshop import (
    RuleCandidate,
    evaluate_candidate,
    valid_evidence,
)
from pydantic import ValidationError


def test_rule_candidate_is_fixed_and_test_vectors_are_separated():
    evidence = evaluate_candidate(RuleCandidate())
    assert evidence["verified"]
    assert len(evidence["sha256"]) == 64
    assert [case["actual"] for case in evidence["test_vectors"]] == [
        "PASS", "FAIL", "UNKNOWN"
    ]
    assert valid_evidence(evidence)
    assert evaluate_candidate(RuleCandidate()) == evidence


def test_mutated_or_agent_injected_rule_cannot_become_approved_evidence():
    with pytest.raises(ValidationError):
        RuleCandidate.model_validate({
            "rule_id": "allow-all",
            "predicate": "exec_code",
            "source_ref": "external",
            "rationale": "Import a different checker without human review",
        })
    with pytest.raises(ValidationError):
        RuleCandidate.model_validate({
            **RuleCandidate().model_dump(),
            "python": "no executable model-supplied code",
        })
    evidence = evaluate_candidate(RuleCandidate())
    evidence["verified"] = False
    assert not valid_evidence(evidence)
    evidence = evaluate_candidate(RuleCandidate())
    evidence["candidate"]["source_ref"] = "untrusted-log"
    assert not valid_evidence(evidence)


@pytest.mark.parametrize("case,field,value", [
    (None, "verified", 1), (0, "complete", 1), (2, "complete", 0),
    (0, "initial_stock", True), (0, "confirmed_count", 1.0),
])
def test_approval_preserves_exact_json_types(case, field, value):
    evidence = evaluate_candidate(RuleCandidate())
    if case is None:
        evidence[field] = value
    elif field == "complete":
        evidence["test_vectors"][case][field] = value
    else:
        evidence["test_vectors"][case]["state"][field] = value
    assert valid_evidence(evidence) is False


@pytest.mark.parametrize("evidence", [None, [], "unexpected"])
def test_workshop_rejects_missing_evidence_objects(evidence):
    assert valid_evidence(evidence) is False
