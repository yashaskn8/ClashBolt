"""Bounded, data-only assessment of preregistered research comparisons.

No target code, model, scripts, or URLs are executed. A matching manifest digest
does not independently authenticate results or prove prior registration.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from clashbolt.evaluation_stats import paired_bootstrap

PROTOCOL = "clashbolt-heldout-comparison-v1"
STRATEGIES = ("semantic", "random", "coverage", "template", "hypothesis")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _names(values: object, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(values, list)
        or (not values and not allow_empty)
        or len(values) > 64
        or any(not isinstance(v, str) or not NAME.fullmatch(v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("Expected bounded unique target IDs")
    return values


def validate_manifest(manifest: dict) -> str:
    if not isinstance(manifest, dict) or set(manifest) != {
        "protocol", "suite_id", "training_targets", "heldout_targets",
        "seeds", "strategies", "cpu_seconds_per_trial", "model_tokens_per_trial",
    }:
        raise ValueError("Unexpected evaluation manifest shape")
    if manifest["protocol"] != PROTOCOL:
        raise ValueError("Unsupported evaluation protocol")
    if not isinstance(manifest["suite_id"], str) or not NAME.fullmatch(
        manifest["suite_id"]
    ):
        raise ValueError("Invalid suite ID")
    training = _names(manifest["training_targets"], allow_empty=True)
    heldout = _names(manifest["heldout_targets"])
    if len(heldout) < 2 or set(training) & set(heldout):
        raise ValueError("Held-out targets require at least two disjoint IDs")
    if manifest["strategies"] != list(STRATEGIES):
        raise ValueError("All five fixed strategies must be declared in order")
    seeds = manifest["seeds"]
    if (
        not isinstance(seeds, list)
        or not 1 <= len(seeds) <= 20
        or any(type(n) is not int or not 0 <= n <= 1_000_000 for n in seeds)
        or len(set(seeds)) != len(seeds)
        or seeds != sorted(seeds)
    ):
        raise ValueError("Invalid fixed experiment seeds")
    cpu = manifest["cpu_seconds_per_trial"]
    tokens = manifest["model_tokens_per_trial"]
    if type(cpu) is not int or not 1 <= cpu <= 3600:
        raise ValueError("Invalid CPU-seconds budget")
    if type(tokens) is not int or not 0 <= tokens <= 1_000_000:
        raise ValueError("Invalid model-token budget")
    return canonical_hash(manifest)


def compare_reported_observations(manifest: dict, submission: dict) -> dict:
    """Fail on missing/duplicate trials; never promote self-reported outcomes."""
    digest = validate_manifest(manifest)
    if (
        not isinstance(submission, dict)
        or set(submission) != {"manifest_sha256", "observations"}
        or submission["manifest_sha256"] != digest
        or not isinstance(submission["observations"], list)
    ):
        raise ValueError("Result set is not bound to the frozen manifest")

    allowed = {
        (target, seed, strategy)
        for target in manifest["heldout_targets"]
        for seed in manifest["seeds"]
        for strategy in STRATEGIES
    }
    observed: dict[tuple[str, int, str], dict] = {}
    for row in submission["observations"]:
        if not isinstance(row, dict) or set(row) != {
            "target_id", "seed", "strategy", "status", "bug_ids",
            "cpu_seconds", "model_tokens", "evidence_sha256",
        }:
            raise ValueError("Unexpected result record shape")
        if (
            not isinstance(row["target_id"], str)
            or type(row["seed"]) is not int
            or not isinstance(row["strategy"], str)
        ):
            raise ValueError("Untrusted observation identifiers")
        key = (row["target_id"], row["seed"], row["strategy"])
        if key not in allowed or key in observed:
            raise ValueError("Unknown, repeated or non-held-out experiment")
        if row["status"] not in ("COMPLETE", "UNKNOWN"):
            raise ValueError("Invalid experiment status")
        ids = _names(row["bug_ids"], allow_empty=True)
        cpu, tokens = row["cpu_seconds"], row["model_tokens"]
        if (
            type(cpu) not in (float, int)
            or not 0 <= cpu <= manifest["cpu_seconds_per_trial"]
            or type(tokens) is not int
            or not 0 <= tokens <= manifest["model_tokens_per_trial"]
        ):
            raise ValueError("Trial exceeded or misreported its budget")
        evidence = row["evidence_sha256"]
        if ids and (
            row["status"] != "COMPLETE"
            or not isinstance(evidence, str)
            or not HASH.fullmatch(evidence)
        ):
            raise ValueError("Reported bug has incomplete or unbound evidence")
        if evidence is not None and (
            not isinstance(evidence, str) or not HASH.fullmatch(evidence)
        ):
            raise ValueError("Malformed evidence identifier")
        observed[key] = row
    if set(observed) != allowed:
        raise ValueError("Missing paired experiments; cannot silently omit failures")

    summary = {}
    per_target = {}
    for strategy in STRATEGIES:
        unique = {
            (target, bug)
            for target in manifest["heldout_targets"]
            for seed in manifest["seeds"]
            for bug in observed[(target, seed, strategy)]["bug_ids"]
        }
        rows = [r for key, r in observed.items() if key[2] == strategy]
        summary[strategy] = {
            "reported_unique_bugs": len(unique),
            "unknown_trials": sum(r["status"] == "UNKNOWN" for r in rows),
            "reported_cpu_seconds": round(sum(r["cpu_seconds"] for r in rows), 4),
            "reported_model_tokens": sum(r["model_tokens"] for r in rows),
            "trials": len(rows),
        }
        positive = {target for target, _ in unique}
        uncertain = {r["target_id"] for r in rows if r["status"] == "UNKNOWN"}
        outcomes = [
            True if target in positive else None if target in uncertain else False
            for target in manifest["heldout_targets"]
        ]
        per_target[strategy] = outcomes
        summary[strategy].update({
            "reported_positive_targets": outcomes.count(True),
            "reported_negative_targets": outcomes.count(False),
            "unresolved_targets": outcomes.count(None),
        })

    strongest = max(
        STRATEGIES[1:],
        key=lambda strategy: summary[strategy]["reported_unique_bugs"],
    )
    uncertainty = {
        strategy: _paired_outcome_bounds(per_target["semantic"], per_target[strategy])
        for strategy in STRATEGIES[1:]
    }
    return {
        "protocol": PROTOCOL,
        "manifest_sha256": digest,
        "heldout_target_ids_reported": len(manifest["heldout_targets"]),
        "paired_by": "target_id",
        "strategies": summary,
        "strongest_reported_baseline": strongest,
        "paired_descriptive_interval": paired_bootstrap(
            [value is True for value in per_target["semantic"]],
            [value is True for value in per_target[strongest]],
        ),
        "paired_descriptive_interval_basis": "reported_findings_only",
        "paired_outcome_uncertainty": uncertainty,
        "costs_are_self_reported": True,
        "independent_reproduction_verified": False,
        "preregistration_timestamp_verified": False,
        "promotion_eligible": False,
        "reason": (
            "A digest and self-reported evidence IDs do not certify independent "
            "reproduction, trusted cost measurements, or pre-registered target splits"
        ),
    }


def _paired_outcome_bounds(candidate: list[bool | None], baseline: list[bool | None]) -> dict:
    """Bound the paired difference without imputing unresolved outcomes."""
    lower = upper = resolved = 0
    for a, b in zip(candidate, baseline, strict=True):
        lower += int(a is True) - int(b is not False)
        upper += int(a is not False) - int(b is True)
        resolved += int(a is not None and b is not None)
    count = len(candidate)
    return {
        "possible_mean_difference": [round(lower / count, 6), round(upper / count, 6)],
        "resolved_pairs": resolved,
        "unresolved_pairs": count - resolved,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Evaluation input exceeds four megabytes")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except RecursionError as error:
        raise ValueError("Evaluation JSON nesting is too deep") from error
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare preregistered, self-reported held-out experiment data"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("observations", type=Path)
    args = parser.parse_args()
    try:
        report = compare_reported_observations(
            _read_json(args.manifest), _read_json(args.observations)
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
