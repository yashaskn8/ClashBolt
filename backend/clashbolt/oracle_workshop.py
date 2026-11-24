"""Constrained inventory-rule workshop. Nothing here evaluates model-supplied code.

A proposed candidate is an *exact* published rule template with explicit fixture
references. Deterministic examples must separate known correct, defective and
incomplete states before an independent reviewer may approve the candidate.
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from clashbolt.experiment import (
    ORACLE_HASH,
    ORACLE_ID,
    ORACLE_RULE,
    ORACLE_VERSION,
    TARGET_ID,
    TARGET_REVISION,
    check_invariant,
)


class RuleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_id: Literal["synthetic-inventory"] = TARGET_ID
    target_revision: Literal["inventory-demo-v1"] = TARGET_REVISION
    rule_id: Literal["confirmed-orders-not-above-stock"] = ORACLE_ID
    predicate: Literal["capacity_and_conservation"] = "capacity_and_conservation"
    source_ref: Literal["fixed-inventory-contract-v1"] = "fixed-inventory-contract-v1"
    rationale: str = Field(
        default="Inventory confirmations cannot exceed available initial stock.",
        min_length=12,
        max_length=240,
    )


def evaluate_candidate(candidate: RuleCandidate) -> dict:
    """Produce immutable evidence for exactly the installed fixed checker."""
    test_vectors = (
        ("correct", {"initial_stock": 1, "remaining_stock": 0, "confirmed_count": 1},
         True, "PASS"),
        ("duplicate", {"initial_stock": 1, "remaining_stock": 0, "confirmed_count": 2},
         True, "FAIL"),
        ("incomplete", {"initial_stock": 1, "remaining_stock": 1, "confirmed_count": 0},
         False, "UNKNOWN"),
    )
    cases = [
        {
            "id": name,
            "state": state,
            "complete": complete,
            "expected": expected,
            "actual": check_invariant(state, complete=complete),
        }
        for name, state, complete, expected in test_vectors
    ]
    verified = all(case["actual"] == case["expected"] for case in cases)
    evidence = {
        "contract": "fixed-inventory-contract-v1",
        "rule_id": candidate.rule_id,
        "rule_version": ORACLE_VERSION,
        "rule_sha256": ORACLE_HASH,
        "canonical_rule": ORACLE_RULE,
        "candidate": candidate.model_dump(),
        "test_vectors": cases,
        "verified": verified,
        "scope": "SHIPPED_SYNTHETIC_FIXTURE_ONLY",
    }
    evidence["sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return evidence


def valid_evidence(evidence: dict | None) -> bool:
    """Prevent approval after candidate, rule or test-vector mutation."""
    if not isinstance(evidence, dict):
        return False
    try:
        candidate = RuleCandidate.model_validate(evidence["candidate"])
        # Python considers True == 1 and 1 == 1.0. Preserve JSON types
        # when comparing review evidence against the canonical test vectors.
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        expected = json.dumps(
            evaluate_candidate(candidate), sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, KeyError, TypeError, RecursionError):
        return False
    return encoded == expected and evidence["verified"] is True
