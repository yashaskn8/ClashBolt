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


def test_mutant_oracle_suite_rejects_every_mutated_predicate_and_verdict():
    from clashbolt.experiment import check_invariant

    valid_state = {"initial_stock": 1, "remaining_stock": 0, "confirmed_count": 1}
    overbooked = {"initial_stock": 1, "remaining_stock": 0, "confirmed_count": 2}
    negative = {"initial_stock": 1, "remaining_stock": -1, "confirmed_count": 2}
    mismatch = {"initial_stock": 1, "remaining_stock": 1, "confirmed_count": 1}
    incomplete = {"initial_stock": 1, "remaining_stock": 1, "confirmed_count": 0}

    assert check_invariant(valid_state, complete=True) == "PASS"
    assert check_invariant(overbooked, complete=True) == "FAIL"
    assert check_invariant(negative, complete=True) == "FAIL"
    assert check_invariant(mismatch, complete=True) == "FAIL"
    assert check_invariant(incomplete, complete=False) == "UNKNOWN"

    mutants = {
        "always_pass": lambda _s, complete: "PASS",
        "inverted_inequality": lambda s, complete: (
            "UNKNOWN" if not complete
            else ("PASS" if s["confirmed_count"] > s["initial_stock"] else "FAIL")
        ),
        "ignore_incomplete": lambda s, _complete: (
            "PASS" if s["confirmed_count"] <= s["initial_stock"] else "FAIL"
        ),
        "ignore_negative": lambda s, complete: (
            "UNKNOWN" if not complete
            else ("PASS" if s["confirmed_count"] <= s["initial_stock"] else "FAIL")
        ),
        "ignore_conservation": lambda s, complete: (
            "UNKNOWN" if not complete
            else ("PASS" if 0 <= s["confirmed_count"] <= s["initial_stock"] else "FAIL")
        ),
    }
    probes = [
        (valid_state, True, "PASS"),
        (overbooked, True, "FAIL"),
        (negative, True, "FAIL"),
        (mismatch, True, "FAIL"),
        (incomplete, False, "UNKNOWN"),
    ]
    killed = 0
    for mutant in mutants.values():
        if any(mutant(state, comp) != expected for state, comp, expected in probes):
            killed += 1
    assert killed == len(mutants)
