from clashbolt.experiment import TARGET_REVISION
from clashbolt.frontier import next_frontier_plan, rank_frontier


def _receipt(schedule: str, verdict: str) -> dict:
    return {
        "plan": {
            "target_revision": TARGET_REVISION,
            "variant": "racy",
            "schedule": schedule,
        },
        "result": {"verdict": verdict},
    }


def test_frontier_is_deterministic_and_explores_the_known_edge():
    empty = rank_frontier([], "racy")
    assert [item["schedule"] for item in empty] == ["overlap", "serial"]
    assert all(item["gate_cost"] == 4 for item in empty)
    history = [_receipt("overlap", "FAIL")] * 7
    ranked = rank_frontier(history, "racy")
    assert ranked[0]["schedule"] == "serial"
    assert ranked[1]["confirmed_failures"] == 7
    plan = next_frontier_plan(history, "racy")
    assert plan["schedule"] == "serial"
    assert plan["max_steps"] == 4
    assert plan["planner_kind"] == "DETERMINISTIC_EVIDENCE_FRONTIER"


def test_unknown_or_external_observations_never_count_as_failures():
    original = rank_frontier([_receipt("overlap", "UNKNOWN")], "racy")
    assert original[0]["confirmed_failures"] == 0
    injected = [
        {
            "plan": {
                "target_revision": "external-target",
                "variant": "racy",
                "schedule": "overlap",
            },
            "result": {"verdict": "FAIL"},
        }
    ]
    assert rank_frontier(injected, "racy") == rank_frontier([], "racy")
