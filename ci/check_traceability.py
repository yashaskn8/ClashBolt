"""Validate file-level TITAN-20 traceability without certifying acceptance."""

import json
import re
import subprocess
from pathlib import Path

TRACKS = {f"B{i}" for i in range(1, 8)} | {f"A{i}" for i in range(1, 8)} | {
    f"P{i}" for i in range(1, 6)
} | {"S1"}
STATES = {"PENDING", "IN_PROGRESS", "VERIFIED", "BLOCKED"}
SHA = re.compile(r"[a-f0-9]{40}\Z")


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
    for row in rows:
        track = row["id"]
        if row["status"] not in STATES or not row["acceptance"].strip():
            raise ValueError(f"Invalid status or acceptance: {track}")
        if not row["pdf_pages"] or any(
            type(page) is not int or not 1 <= page <= 28 for page in row["pdf_pages"]
        ):
            raise ValueError(f"Invalid PDF references: {track}")
        deps = row["dependencies"]
        if len(deps) != len(set(deps)) or track in deps or not set(deps) <= TRACKS:
            raise ValueError(f"Invalid dependencies: {track}")
        if row["status"] == "VERIFIED":
            if row["blockers"] or not row["evidence"] or any(
                not SHA.fullmatch(item.get("commit", ""))
                or type(item.get("run_id")) is not int or item["run_id"] <= 0
                or not item.get("acceptance_record", "").strip()
                for item in row["evidence"]
            ):
                raise ValueError(f"Unsubstantiated verification: {track}")
        if row["status"] == "BLOCKED" and not row["blockers"]:
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
