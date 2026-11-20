import asyncio

import pytest
from clashbolt.experiment import check_invariant, execute


@pytest.mark.parametrize(
    ("variant", "schedule", "expected"),
    [
        ("racy", "overlap", "FAIL"),
        ("racy", "serial", "PASS"),
        ("locked", "overlap", "PASS"),
        ("locked", "serial", "PASS"),
    ],
)
def test_schedule_and_oracle(variant, schedule, expected):
    result = asyncio.run(execute(variant, schedule))
    assert result["verdict"] == expected
    assert result["complete"]
    assert len(result["observed_steps"]) == 4
    assert [f"{s['client']}.{s['checkpoint']}" for s in result["observed_steps"]] == result[
        "requested_gates"
    ]


def test_isolation_and_stable_replay():
    first = asyncio.run(execute("racy", "overlap"))
    second = asyncio.run(execute("racy", "overlap"))
    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["final_state"] == second["final_state"]
    assert second["fixture"]["initial_stock"] == 1


def test_oracle_abstains_on_missing_evidence():
    assert check_invariant(None, complete=False) == "UNKNOWN"
    assert check_invariant({"initial_stock": 1}, complete=True) == "UNKNOWN"
    incomplete = {"initial_stock": 1, "remaining_stock": 0, "confirmed_count": 2}
    assert check_invariant(incomplete, complete=False) == "UNKNOWN"


def test_unknown_targets_cannot_execute():
    with pytest.raises(ValueError):
        asyncio.run(execute("network", "overlap"))
