import pytest
from pydantic import ValidationError

from clashbolt.oracle_workshop import (
    RuleCandidate,
    evaluate_candidate,
    valid_evidence,
)


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
