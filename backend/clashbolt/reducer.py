"""Bounded, evidence-checked gate deletion for the shipped inventory fixture.

Every candidate is replayed against a freshly reset fixture. A native SQL
witness requires a separate disposable PostgreSQL target per trial.
This does not claim a generalized delta-debugger or ungated reproduction.
"""

import hashlib
import json
from uuid import uuid4

from clashbolt.experiment import (
    ORACLE_HASH,
    SCHEDULES,
    InventoryFixture,
    PostgresInventoryFixture,
    check_invariant,
)
from clashbolt.target_db import disposable_target
from clashbolt.target_service import request_fixture, service_enabled
from clashbolt.witness import validate_contract


def _probe(variant: str, gates: list[str], target_url: str | None) -> str:
    fixture = PostgresInventoryFixture(target_url) if target_url else InventoryFixture()
    observed: dict[str, int] = {}
    for label in gates:
        client, action = label.split(".")
        if action == "read":
            observed[client] = fixture.read(client)["observed_stock"]
        elif client in observed:
            fixture.commit(client, observed.pop(client), variant)
        else:
            return "UNKNOWN"  # Missing evidence for a claimed commit.
    return check_invariant(fixture.final_state(), complete=True)


def _trial(variant: str, schedule: str, gates: list[str], target_url: str | None) -> str:
    if service_enabled():
        response = request_fixture(variant, schedule, target_url, gates)
        verdict = response.get("verdict")
        if verdict not in ("PASS", "FAIL", "UNKNOWN"):
            raise ValueError("Invalid target service probe verdict")
        return verdict
    return _probe(variant, gates, target_url)


def reduce_witness(
    witness: dict, target_admin_url: str | None = None, control_url: str | None = None
) -> dict:
    invalid = validate_contract(witness)
    if invalid:
        return {"status": "INVALID", "reason": invalid}
    if not witness["complete"] or witness["verdict"] != "FAIL":
        return {"status": "UNCONFIRMED", "reason": "Only completed failures can be reduced"}

    backend = witness.get("execution_backend", "memory")
    if backend == "native-postgresql" and (not target_admin_url or not control_url):
        return {"status": "UNCONFIRMED", "reason": "Native test cluster is required"}

    remaining = list(SCHEDULES[witness["schedule"]])
    trials = []
    max_trials = 16
    changed = True
    while changed and len(trials) < max_trials:
        changed = False
        for index in range(len(remaining)):
            if len(trials) >= max_trials:
                break
            candidate = remaining[:index] + remaining[index + 1:]
            if backend == "native-postgresql":
                with disposable_target(
                    target_admin_url, control_url, uuid4()
                ) as fresh:
                    verdict = _trial(witness["variant"], witness["schedule"], candidate, fresh.url)
            else:
                verdict = _trial(witness["variant"], witness["schedule"], candidate, None)
            accepted = verdict == "FAIL"
            trials.append({
                "removed_gate": remaining[index],
                "candidate_gates": candidate,
                "verdict": verdict,
                "accepted": accepted,
            })
            if accepted:
                remaining = candidate
                changed = True
                break
    complete_search = not changed and len(trials) < max_trials
    report = {
        "status": "MINIMAL" if complete_search else "BOUNDED",
        "scope": "SYNTHETIC_ONLY",
        "original_evidence_sha256": witness["evidence_sha256"],
        "approved_oracle_sha256": ORACLE_HASH,
        "execution_backend": backend,
        "original_gates": list(SCHEDULES[witness["schedule"]]),
        "minimal_gates": remaining,
        "original_gate_count": len(SCHEDULES[witness["schedule"]]),
        "minimal_gate_count": len(remaining),
        "reduction_trials": trials,
        "supported_reductions": "single application-gate deletion",
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report
