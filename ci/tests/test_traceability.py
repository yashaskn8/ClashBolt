"""Trace links are checked separately from operational acceptance."""

import json
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


def test_cli_reads_tracked_inventory(inputs, monkeypatch, capsys):
    from ci import check_traceability

    _, tracked = inputs
    monkeypatch.setattr(check_traceability.subprocess, "check_output", lambda *a, **k:
                        "\0".join(sorted(tracked)) + "\0")
    check_traceability.main()
    output = json.loads(capsys.readouterr().out)
    assert output["tracks"] == 20
    assert set(output["reverse_index"]) == tracked
