"""Validate file-level TITAN-20 traceability without certifying acceptance."""

import json
import re
import subprocess
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

TRACKS = {f"B{i}" for i in range(1, 8)} | {f"A{i}" for i in range(1, 8)} | {
    f"P{i}" for i in range(1, 6)
} | {"S1"}
CI_STATES = {"CI_PENDING", "CI_FAILED", "VERIFIED", "PRODUCTION_READY"}
EVIDENCE_STATES = CI_STATES | {"LOCALLY_VERIFIED"}
ACCEPTED_STATES = {"VERIFIED", "PRODUCTION_READY"}
STATES = EVIDENCE_STATES | {"PENDING", "IN_PROGRESS", "BLOCKED"}
SHA = re.compile(r"[a-f0-9]{40}\Z")


def _valid_evidence(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    stage = item.get("stage", "VERIFIED")
    commit = item.get("commit")
    record = item.get("acceptance_record")
    if (
        not isinstance(stage, str) or stage not in EVIDENCE_STATES
        or not isinstance(commit, str) or not SHA.fullmatch(commit)
        or not isinstance(record, str) or not record.strip()
    ):
        return False
    if stage in CI_STATES:
        run_id = item.get("run_id")
        if type(run_id) is not int or run_id <= 0:
            return False
    if stage == "PRODUCTION_READY":
        acceptance = item.get("production_acceptance_record")
        return isinstance(acceptance, str) and bool(acceptance.strip())
    return True


def validate(ledger: dict, tracked: set[str]) -> dict[str, list[str]]:
    if ledger.get("revision") != "2026-11-03":
        raise ValueError("Unexpected specification revision")
    if not SHA.fullmatch(ledger.get("reviewed_head", "")):
        raise ValueError("Missing reviewed commit")
    baseline = ledger["last_successful_ci"]
    if not SHA.fullmatch(baseline.get("commit", "")) or (
        type(baseline.get("run_id")) is not int or baseline["run_id"] <= 0
    ):
        raise ValueError("Invalid CI baseline reference")
    rows = ledger["requirements"]
    ids = [row["id"] for row in rows]
    if len(ids) != len(TRACKS) or set(ids) != TRACKS:
        raise ValueError("Expected exactly the twenty TITAN-20 tracks")
    reverse: dict[str, list[str]] = {}
    dependencies: dict[str, list[str]] = {}
    for row in rows:
        track = row["id"]
        status = row["status"]
        if status not in STATES or not row["acceptance"].strip():
            raise ValueError(f"Invalid status or acceptance: {track}")
        if not row["pdf_pages"] or any(
            type(page) is not int or not 1 <= page <= 28 for page in row["pdf_pages"]
        ):
            raise ValueError(f"Invalid PDF references: {track}")
        deps = row["dependencies"]
        if len(deps) != len(set(deps)) or track in deps or not set(deps) <= TRACKS:
            raise ValueError(f"Invalid dependencies: {track}")
        dependencies[track] = deps
        if status in EVIDENCE_STATES:
            evidence = row["evidence"]
            if (
                (status in ACCEPTED_STATES and row["blockers"])
                or not isinstance(evidence, list)
                or not all(_valid_evidence(item) for item in evidence)
                or not any(item.get("stage", "VERIFIED") == status for item in evidence)
            ):
                raise ValueError(f"Unsubstantiated verification: {track}")
        if status == "BLOCKED" and not row["blockers"]:
            raise ValueError(f"Missing blocker: {track}")
        for kind in ("code", "tests"):
            paths = row[kind]
            if not paths or len(paths) != len(set(paths)):
                raise ValueError(f"Missing or duplicate {kind} links: {track}")
            for path in paths:
                prefixes = ("backend/clashbolt/", "frontend/src/") if kind == "code" else (
                    "backend/tests/", "frontend/e2e/", "ci/tests/"
                )
                if not path.startswith(prefixes) or path not in tracked:
                    raise ValueError(f"Untracked or misplaced {kind} link: {track}: {path}")
                reverse.setdefault(path, []).append(track)
    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as error:
        cycle = " -> ".join(error.args[1])
        raise ValueError(f"Cyclic TITAN-20 dependencies: {cycle}") from error
    return {path: sorted(ids) for path, ids in sorted(reverse.items())}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, text=True,
    ).split("\0"))
    ledger = json.loads((root / "docs/TITAN20_TRACEABILITY.json").read_text(encoding="utf-8"))
    reverse = validate(ledger, tracked)
    print(json.dumps({"tracks": len(TRACKS), "reverse_index": reverse}, sort_keys=True))


if __name__ == "__main__":
    main()
