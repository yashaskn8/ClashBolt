"""Deterministic adversarial checks of the shipped *synthetic* fixture only.

No external target, active network scanning, commands supplied by a user, or
real credentials. Rehashed forged evidence must never become a valid finding.
"""

import asyncio
import json
from copy import deepcopy

import pytest
from clashbolt.experiment import execute
from clashbolt.target_service import run_request
from clashbolt.witness import evidence_digest, validate_contract, verify


def _corrupt(witness, index):
    kind = index % 17
    changes = {
        0: lambda w: w.update(scope="EXTERNAL"),
        1: lambda w: w.update(target_id="untrusted-target"),
        2: lambda w: w.update(fixture_hash="0" * 64),
        3: lambda w: w["oracle"].update(sha256="f" * 64),
        4: lambda w: w.update(variant="invented-backend"),
        5: lambda w: w.update(schedule="unbounded"),
        6: lambda w: w["requested_gates"].reverse(),
        7: lambda w: w["observed_steps"].pop(),
        8: lambda w: w["observed_steps"][0].update(sequence=index + 5),
        9: lambda w: w["observed_steps"][2].update(accepted=False),
        10: lambda w: w["final_state"].update(confirmed_count=index + 9),
        11: lambda w: w["observed_steps"][0].update(observed_stock=index + 12),
        12: lambda w: w["observed_steps"][0].update(program="untrusted"),
        13: lambda w: w.update(execution_backend="external"),
        14: lambda w: w.update(target_revision="unreviewed"),
        15: lambda w: w.update(complete="true"),
        16: lambda w: w["observed_steps"][0].update(observed_stock=True),
    }
    changes[kind](witness)
    witness["evidence_sha256"] = evidence_digest(witness)


def test_272_forged_histories_rejected_even_with_recomputed_hash():
    authentic = asyncio.run(execute("racy", "overlap"))
    assert validate_contract(authentic) is None
    for index in range(272):
        forged = deepcopy(authentic)
        _corrupt(forged, index)
        reason = validate_contract(forged)
        assert reason is not None, f"Mutated witness {index} passed validation"
        if index < 34:
            result = asyncio.run(verify(forged))
            assert result["status"] == "INVALID", (index, result)


def test_160_malformed_fixed_fixture_requests_are_rejected():
    safe = {"variant": "locked", "schedule": "serial"}
    for index in range(160):
        request = dict(safe)
        case = index % 8
        if case == 0:
            request["url"] = "https://example.invalid/synthetic"
        elif case == 1:
            request["program"] = f"never-accepted-{index}"
        elif case == 2:
            request["variant"] = f"unspecified-{index}"
        elif case == 3:
            request["schedule"] = ["serial", index]
        elif case == 4:
            request["gates"] = ["a.commit", "a.read"]
        elif case == 5:
            request["gates"] = ["a.read", "a.read"]
        elif case == 6:
            request["login"] = {
                "instance": "a" * 32, "password": "p" * 43,
                "unexpected": index,
            }
        else:
            request["gates"] = ["foreign-" + str(index)]
        with pytest.raises(ValueError):
            run_request(json.dumps(request).encode())
