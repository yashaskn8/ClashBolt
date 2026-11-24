"""Off-line research gate never treats unverified outcomes as evidence."""

from copy import deepcopy

import pytest
from clashbolt.heldout import (
    PROTOCOL,
    STRATEGIES,
    canonical_hash,
    compare_reported_observations,
    validate_manifest,
)


def example():
    manifest = {
        "protocol": PROTOCOL,
        "suite_id": "research-test-fixtures",
        "training_targets": ["known-inventory"],
        "heldout_targets": ["sample-a", "sample-b"],
        "seeds": [0, 1],
        "strategies": list(STRATEGIES),
        "cpu_seconds_per_trial": 60,
        "model_tokens_per_trial": 256,
    }
    observations = []
    for target in manifest["heldout_targets"]:
        for seed in manifest["seeds"]:
            for strategy in STRATEGIES:
                bug_ids = ["bug-1"] if (
                    target == "sample-a" and strategy == "semantic" and seed == 0
                ) else []
                observations.append({
                    "target_id": target,
                    "seed": seed,
                    "strategy": strategy,
                    "status": "COMPLETE",
                    "bug_ids": bug_ids,
                    "cpu_seconds": 3.25,
                    "model_tokens": 12 if strategy == "semantic" else 0,
                    "evidence_sha256": "a" * 64 if bug_ids else None,
                })
    return manifest, {
        "manifest_sha256": canonical_hash(manifest),
        "observations": observations,
    }


def test_report_is_paired_bounded_and_never_promoted():
    manifest, results = example()
    report = compare_reported_observations(manifest, results)
    assert report["heldout_target_ids_reported"] == 2
    assert report["paired_descriptive_interval"]["observations"] == 2
    assert report["strategies"]["semantic"]["reported_unique_bugs"] == 1
    assert report["strategies"]["random"]["reported_unique_bugs"] == 0
    assert report["strategies"]["semantic"]["reported_model_tokens"] == 48
    assert report["strongest_reported_baseline"] == "random"
    assert report["promotion_eligible"] is False
    assert report["independent_reproduction_verified"] is False
    assert report == compare_reported_observations(manifest, results)


def test_malformed_or_overlapping_manifest_cannot_be_reported():
    manifest, results = example()
    manifest["heldout_targets"].append("known-inventory")
    with pytest.raises(ValueError, match="disjoint"):
        compare_reported_observations(manifest, results)
    manifest, results = example()
    manifest["strategies"] = ["semantic", "random"]
    with pytest.raises(ValueError, match="five"):
        validate_manifest(manifest)
    manifest, results = example()
    manifest["seeds"] = [0, 0]
    with pytest.raises(ValueError, match="seeds"):
        validate_manifest(manifest)


def test_changed_manifest_and_missing_or_duplicate_rows_rejected():
    manifest, submitted = example()
    altered = deepcopy(manifest)
    altered["cpu_seconds_per_trial"] = 1
    with pytest.raises(ValueError, match="frozen manifest"):
        compare_reported_observations(altered, submitted)
    for mutation in ("omit", "duplicate"):
        modified = deepcopy(submitted)
        if mutation == "omit":
            modified["observations"].pop()
        else:
            modified["observations"].append(modified["observations"][0])
        with pytest.raises(ValueError, match="Missing paired|repeated"):
            compare_reported_observations(manifest, modified)


def test_unknown_or_unbound_bug_never_counts_as_confirmed():
    manifest, submitted = example()
    changed = deepcopy(submitted)
    flagged = next(r for r in changed["observations"] if r["bug_ids"])
    flagged["status"] = "UNKNOWN"
    with pytest.raises(ValueError, match="incomplete"):
        compare_reported_observations(manifest, changed)
    flagged["status"] = "COMPLETE"
    flagged["evidence_sha256"] = None
    with pytest.raises(ValueError, match="unbound"):
        compare_reported_observations(manifest, changed)


def test_budget_limits_and_nonfinite_values_rejected():
    manifest, submitted = example()
    for value in (61, float("nan"), float("inf"), -1):
        changed = deepcopy(submitted)
        changed["observations"][0]["cpu_seconds"] = value
        with pytest.raises(ValueError, match="budget"):
            compare_reported_observations(manifest, changed)
    changed = deepcopy(submitted)
    changed["observations"][0]["model_tokens"] = 257
    with pytest.raises(ValueError, match="budget"):
        compare_reported_observations(manifest, changed)


def test_heldout_rows_cannot_be_replaced_with_training_rows():
    manifest, submitted = example()
    changed = deepcopy(submitted)
    changed["observations"][0]["target_id"] = "known-inventory"
    with pytest.raises(ValueError, match="non-held-out"):
        compare_reported_observations(manifest, changed)



def test_cli_reads_local_json_and_reports_no_promotion(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from clashbolt.heldout import main

    manifest, submitted = example()
    first, second = tmp_path / "manifest.json", tmp_path / "results.json"
    first.write_text(json.dumps(manifest), encoding="utf-8")
    second.write_text(json.dumps(submitted), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["clashbolt-heldout", str(first), str(second)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["promotion_eligible"] is False
    assert report["manifest_sha256"] == canonical_hash(manifest)


def test_observation_identifier_types_are_checked_before_dict_keys():
    manifest, submitted = example()
    for field, invalid in (
        ("target_id", []),
        ("seed", True),
        ("strategy", {}),
    ):
        changed = deepcopy(submitted)
        changed["observations"][0][field] = invalid
        with pytest.raises(ValueError, match="Untrusted observation identifiers"):
            compare_reported_observations(manifest, changed)


@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_huge_integer_cpu_cost_is_rejected_as_invalid_budget(value):
    manifest, submitted = example()
    submitted["observations"][0]["cpu_seconds"] = value
    with pytest.raises(ValueError, match="budget"):
        compare_reported_observations(manifest, submitted)


@pytest.mark.parametrize("duplicate_in", ["manifest", "observations"])
def test_cli_rejects_duplicate_json_keys(tmp_path, monkeypatch, capsys, duplicate_in):
    import json
    import sys

    from clashbolt.heldout import main

    manifest, submitted = example()
    first, second = tmp_path / "manifest.json", tmp_path / "results.json"
    manifest_json, observations_json = json.dumps(manifest), json.dumps(submitted)
    if duplicate_in == "manifest":
        manifest_json = manifest_json.replace(
            '"suite_id":', '"suite_id": "overwritten", "suite_id":', 1
        )
    else:
        observations_json = observations_json.replace(
            '"cpu_seconds":', '"cpu_seconds": 999, "cpu_seconds":', 1
        )
    first.write_text(manifest_json, encoding="utf-8")
    second.write_text(observations_json, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["clashbolt-heldout", str(first), str(second)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "Duplicate JSON object key" in capsys.readouterr().err


@pytest.mark.parametrize("payload, message", [
    (b" " * 4_000_001, "exceeds four megabytes"),
    (b"[]", "Expected a JSON object"),
    (b"{", "Expecting property name"),
])
def test_cli_rejects_invalid_input_files(tmp_path, monkeypatch, capsys, payload, message):
    import sys

    from clashbolt.heldout import main

    path = tmp_path / "invalid.json"
    path.write_bytes(payload)
    monkeypatch.setattr(sys, "argv", ["clashbolt-heldout", str(path), str(path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("field, value, message", [
    ("protocol", "unknown", "Unsupported"),
    ("suite_id", [], "Invalid suite"),
    ("heldout_targets", ["sample-a", "sample-a"], "unique target"),
    ("cpu_seconds_per_trial", True, "CPU-seconds"),
    ("model_tokens_per_trial", -1, "model-token"),
])
def test_invalid_manifest_fields_are_rejected(field, value, message):
    manifest, _ = example()
    manifest[field] = value
    with pytest.raises(ValueError, match=message):
        validate_manifest(manifest)


@pytest.mark.parametrize("field, value, message", [
    ("status", "RUNNING", "Invalid experiment status"),
    ("evidence_sha256", "not-a-digest", "Malformed evidence"),
])
def test_invalid_observation_fields_are_rejected(field, value, message):
    manifest, submitted = example()
    row = next(row for row in submitted["observations"] if not row["bug_ids"])
    row[field] = value
    with pytest.raises(ValueError, match=message):
        compare_reported_observations(manifest, submitted)



def test_unexpected_manifest_and_observation_shapes_are_rejected():
    with pytest.raises(ValueError, match="manifest shape"):
        validate_manifest({})
    manifest, submitted = example()
    submitted["observations"][0] = {}
    with pytest.raises(ValueError, match="result record shape"):
        compare_reported_observations(manifest, submitted)
