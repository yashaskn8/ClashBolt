"""Independently re-run a shipped synthetic witness from exported JSON.

The verifier never imports or executes user-supplied code and accepts no network target.
"""

import argparse
import asyncio
import json
from hashlib import sha256
from pathlib import Path

from clashbolt.experiment import (
    FIXTURE,
    FIXTURE_HASH,
    ORACLE_HASH,
    ORACLE_ID,
    ORACLE_VERSION,
    SCHEDULES,
    TARGET_ID,
    TARGET_REVISION,
    check_invariant,
    execute,
)


def evidence_digest(witness: dict) -> str:
    payload = {k: v for k, v in witness.items() if k not in ("evidence_sha256", "scope")}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(serialized).hexdigest()


def validate_contract(witness: dict) -> str | None:
    """Return a reason for rejection; a valid digest alone is not a valid verdict."""
    if not isinstance(witness, dict):
        return "Expected a JSON object"
    oracle = witness.get("oracle")
    if not isinstance(oracle, dict):
        return "Missing immutable oracle binding"
    if (
        witness.get("scope") != "SYNTHETIC_ONLY"
        or witness.get("target_id") != TARGET_ID
        or witness.get("target_revision") != TARGET_REVISION
        or witness.get("fixture") != FIXTURE
        or witness.get("fixture_hash") != FIXTURE_HASH
        or oracle.get("id") != ORACLE_ID
        or oracle.get("sha256") != ORACLE_HASH
        or oracle.get("version") != ORACLE_VERSION
        or witness.get("variant") not in ("racy", "locked")
        or witness.get("schedule") not in SCHEDULES
    ):
        return "Witness does not match the fixed demo contracts"
    try:
        actual_digest = evidence_digest(witness)
    except (TypeError, ValueError):
        return "Witness cannot be serialized"
    if actual_digest != witness.get("evidence_sha256"):
        return "Evidence digest differs from the exported record"

    observed = witness.get("observed_steps")
    expected = SCHEDULES[witness["schedule"]]
    if not isinstance(observed, list) or len(observed) > len(expected):
        return "Invalid observed checkpoint sequence"
    if not all(
        isinstance(step, dict)
        and step.get("sequence") == i + 1
        and step.get("client") == expected[i].split(".")[0]
        and step.get("checkpoint") == expected[i].split(".")[1]
        for i, step in enumerate(observed)
    ):
        return "Checkpoint observations disagree with the declared schedule"
    if witness.get("requested_gates") != list(expected):
        return "Requested gate order was changed"
    if witness.get("complete") is True and len(observed) != len(expected):
        return "Completed witness is missing checkpoints"

    state = witness.get("final_state")
    if not isinstance(state, dict):
        return "Missing authoritative fixture state"
    computed = check_invariant(state, complete=witness.get("complete") is True)
    if computed != witness.get("verdict"):
        return "Recorded verdict disagrees with the independent oracle"
    return None


async def verify(witness: dict) -> dict:
    """Three states: INVALID, REPRODUCED, or UNCONFIRMED."""
    rejected = validate_contract(witness)
    if rejected:
        return {"status": "INVALID", "reason": rejected}
    if not witness.get("complete"):
        return {"status": "UNCONFIRMED", "reason": "Original controlled run was incomplete"}

    replay = await execute(witness["variant"], witness["schedule"])
    matched = (
        replay["complete"]
        and replay["evidence_sha256"] == witness["evidence_sha256"]
        and replay["verdict"] in ("PASS", "FAIL")
    )
    return {
        "status": "REPRODUCED" if matched else "UNCONFIRMED",
        "scope": "SYNTHETIC_ONLY",
        "verdict": replay["verdict"] if matched else "UNKNOWN",
        "original_sha256": witness["evidence_sha256"],
        "replay_sha256": replay["evidence_sha256"],
        "reason": (
            "Fresh Python process can replay the declared application checkpoints"
            if matched else "Controlled outcome differs on fresh fixture"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a ClashBolt synthetic JSON witness")
    parser.add_argument("witness", type=Path, help="Exported JSON witness file")
    args = parser.parse_args()
    if not args.witness.is_file() or args.witness.stat().st_size > 100_000:
        parser.error("Witness must exist and be 100 KB or smaller")
    try:
        value = json.loads(args.witness.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        parser.error(f"Cannot parse witness JSON: {error}")
    result = asyncio.run(verify(value))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "REPRODUCED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
