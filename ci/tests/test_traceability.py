"""Trace links are checked separately from operational acceptance."""

import json
import runpy
from pathlib import Path

import pytest

from ci.check_traceability import TRACKS, validate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inputs():
    ledger = json.loads((ROOT / "docs/TITAN20_TRACEABILITY.json").read_text())
    tracked = {path for row in ledger["requirements"] for key in ("code", "tests")
               for path in row[key]}
    return ledger, tracked


def test_reverse_index(inputs):
    ledger, tracked = inputs
    reverse = validate(ledger, tracked)
    assert set(reverse) == tracked
    assert {track for tracks in reverse.values() for track in tracks} == TRACKS
    assert "B1" in reverse["backend/clashbolt/store.py"]
    assert "B7" in reverse["backend/clashbolt/store.py"]


@pytest.mark.parametrize("mutation", [
    "revision", "commit", "missing", "duplicate", "status", "acceptance", "page",
    "dependency", "self", "verified", "blocked", "empty", "duplicate_path", "untracked",
    "wrong_kind", "ci_baseline", "unsubstantiated_evidence",
])
def test_invalid_links_and_claims_are_rejected(inputs, mutation):
    ledger, tracked = inputs
    rows = ledger["requirements"]
    row = rows[0]
    if mutation == "revision":
        ledger["revision"] = "unknown"
    elif mutation == "commit":
        ledger["reviewed_head"] = "main"
    elif mutation == "ci_baseline":
        ledger["last_successful_ci"]["run_id"] = True
    elif mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = row
    elif mutation == "status":
        row["status"] = "COMPLETE"
    elif mutation == "acceptance":
        row["acceptance"] = " "
    elif mutation == "page":
        row["pdf_pages"] = [True]
    elif mutation == "dependency":
        row["dependencies"] = ["unknown"]
    elif mutation == "self":
        row["dependencies"] = [row["id"]]
    elif mutation == "verified":
        row["status"] = "VERIFIED"
    elif mutation == "unsubstantiated_evidence":
        row["status"] = "VERIFIED"
        row["blockers"] = []
        row["evidence"] = [{"commit": "main", "run_id": 1}]
    elif mutation == "blocked":
        row["status"] = "BLOCKED"
        row["blockers"] = []
    elif mutation == "empty":
        row["tests"] = []
    elif mutation == "duplicate_path":
        row["code"].append(row["code"][0])
    elif mutation == "untracked":
        tracked.remove(row["code"][0])
    else:
        row["code"] = row["tests"]
    with pytest.raises(ValueError):
        validate(ledger, tracked)


def test_structurally_valid_evidence_is_only_a_reference(inputs):
    ledger, tracked = inputs
    row = ledger["requirements"][0]
    row["status"] = "VERIFIED"
    row["blockers"] = []
    row["evidence"] = [{
        "commit": "a" * 40, "run_id": 123, "acceptance_record": "synthetic reference",
    }]
    assert "B1" in validate(ledger, tracked)[row["code"][0]]


@pytest.mark.parametrize("stage, extra", [
    ("LOCALLY_VERIFIED", {}),
    ("CI_PENDING", {"run_id": 123}),
    ("CI_FAILED", {"run_id": 123}),
    ("PRODUCTION_READY", {
        "run_id": 123, "production_acceptance_record": "independent-acceptance-001",
    }),
])
def test_verification_stage_requires_its_own_evidence(inputs, stage, extra):
    ledger, tracked = inputs
    row = ledger["requirements"][0]
    row["status"] = stage
    row["blockers"] = [] if stage == "PRODUCTION_READY" else ["External acceptance outstanding"]
    row["evidence"] = [{
        "stage": "LOCALLY_VERIFIED", "commit": "a" * 40, "acceptance_record": "focused native test",
    }, {
        "stage": stage, "commit": "b" * 40, "acceptance_record": "stage-specific record", **extra,
    }]
    assert "B1" in validate(ledger, tracked)[row["code"][0]]


@pytest.mark.parametrize(("stage", "change"), [
    ("LOCALLY_VERIFIED", {"commit": False}),
    ("LOCALLY_VERIFIED", {"acceptance_record": " "}),
    ("CI_PENDING", {"run_id": None}),
    ("CI_FAILED", {"stage": "LOCALLY_VERIFIED"}),
    ("CI_FAILED", {"run_id": True}),
    ("CI_FAILED", {"run_id": 0}),
    ("PRODUCTION_READY", {"production_acceptance_record": " "}),
    ("PRODUCTION_READY", {"stage": "VERIFIED"}),
    ("VERIFIED", {"stage": "not a verification stage"}),
])
def test_rejects_evidence_that_does_not_support_the_current_stage(inputs, stage, change):
    ledger, tracked = inputs
    row = ledger["requirements"][0]
    row["status"] = stage
    row["blockers"] = []
    row["evidence"] = [{
        "stage": stage, "commit": "a" * 40, "run_id": 123, "acceptance_record": "focused record",
        "production_acceptance_record": "independent-acceptance-001", **change,
    }]
    with pytest.raises(ValueError, match="Unsubstantiated verification: B1"):
        validate(ledger, tracked)


def test_production_ready_rejects_open_blockers(inputs):
    ledger, tracked = inputs
    row = ledger["requirements"][0]
    row["status"] = "PRODUCTION_READY"
    row["evidence"] = [{
        "stage": "PRODUCTION_READY", "commit": "a" * 40, "run_id": 123,
        "acceptance_record": "mandatory gates", "production_acceptance_record": "accepted",
    }]
    with pytest.raises(ValueError, match="Unsubstantiated verification: B1"):
        validate(ledger, tracked)


@pytest.mark.parametrize("evidence", [None, ["unstructured reference"]])
def test_verification_rejects_unstructured_evidence(inputs, evidence):
    ledger, tracked = inputs
    row = ledger["requirements"][0]
    row["status"] = "LOCALLY_VERIFIED"
    row["evidence"] = evidence
    with pytest.raises(ValueError, match="Unsubstantiated verification: B1"):
        validate(ledger, tracked)


@pytest.mark.parametrize("dependencies", [
    {"B1": ["B4"], "B4": ["B1"]},
    {"B1": ["B4"], "B4": ["B5"], "B5": ["B1"]},
    {"A1": ["A2"], "A2": ["A3"], "A3": ["A1"]},
])
def test_dependency_cycles_are_rejected(inputs, dependencies):
    ledger, tracked = inputs
    for row in ledger["requirements"]:
        if row["id"] in dependencies:
            row["dependencies"] = dependencies[row["id"]]
    with pytest.raises(ValueError, match="^Cyclic TITAN-20 dependencies: ") as error:
        validate(ledger, tracked)
    cycle = str(error.value).split(": ", 1)[1].split(" -> ")
    assert cycle[0] == cycle[-1]
    assert set(cycle[:-1]) == dependencies.keys()
    assert all(prerequisite in dependencies[dependent]
               for prerequisite, dependent in zip(cycle, cycle[1:]))


def test_shared_prerequisites_are_valid_in_any_row_order(inputs):
    ledger, tracked = inputs
    expected = validate(ledger, tracked)
    ledger["requirements"].reverse()
    assert validate(ledger, tracked) == expected


def test_duplicate_dependency_is_invalid_even_without_a_cycle(inputs):
    ledger, tracked = inputs
    ledger["requirements"][0]["dependencies"] = ["B4", "B4"]
    with pytest.raises(ValueError, match="Invalid dependencies: B1"):
        validate(ledger, tracked)


def test_cli_reads_tracked_inventory(inputs, monkeypatch, capsys):
    from ci import check_traceability

    _, tracked = inputs
    monkeypatch.setattr(check_traceability.subprocess, "check_output", lambda *a, **k:
                        "\0".join(sorted(tracked)) + "\0")
    runpy.run_path(str(ROOT / "ci/check_traceability.py"), run_name="__main__")
    output = json.loads(capsys.readouterr().out)
    assert output["tracks"] == 20
    assert set(output["reverse_index"]) == tracked
