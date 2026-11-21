"""Independently re-run a shipped synthetic witness from exported JSON.

The verifier never imports or executes user-supplied code and accepts no network target.
"""

import argparse
import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Callable, ContextManager

import psycopg

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
    execute_postgres,
)
from clashbolt.target_db import TargetLease, disposable_target
from clashbolt.target_service import request_fixture, service_enabled


def evidence_digest(witness: dict) -> str:
    payload = {k: v for k, v in witness.items() if k not in ("evidence_sha256", "scope")}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(serialized).hexdigest()


def _history_error(witness: dict) -> str | None:
    """Replay every declared action without trusting reported reads or commits."""
    gates = SCHEDULES[witness["schedule"]]
    observed = witness.get("observed_steps")
    if not isinstance(observed, list) or len(observed) > len(gates):
        return "Invalid observed checkpoint count"
    if witness.get("requested_gates") != list(gates):
        return "Requested gates do not match the selected schedule"
    if witness.get("complete") is True and len(observed) != len(gates):
        return "Completed witness is missing checkpoints"

    remaining = FIXTURE["initial_stock"]
    confirmed = 0
    reads: dict[str, int] = {}
    for index, event in enumerate(observed):
        client, phase = gates[index].split(".")
        if not isinstance(event, dict):
            return "Checkpoint must be a structured observation"
        if (
            type(event.get("sequence")) is not int
            or event["sequence"] != index + 1
            or event.get("client") != client
            or event.get("checkpoint") != phase
        ):
            return "Checkpoint order or identity differs from the schedule"

        if phase == "read":
            if set(event) != {"sequence", "client", "checkpoint", "observed_stock"}:
                return "Unexpected fields in a stock read"
            if type(event.get("observed_stock")) is not int:
                return "A stock read must be an integer"
            if event["observed_stock"] != remaining:
                return "Reported read does not match the preceding fixture state"
            reads[client] = remaining
        else:
            if set(event) != {
                "sequence", "client", "checkpoint",
                "accepted", "stock_after", "confirmed_after",
            }:
                return "Unexpected fields in a commit"
            if client not in reads:
                return "Commit lacks its preceding read"
            available = (
                reads.pop(client) if witness["variant"] == "racy" else remaining
            )
            accepted = available >= 1
            if accepted:
                remaining = available - 1
                confirmed += 1
            if (
                type(event.get("accepted")) is not bool
                or type(event.get("stock_after")) is not int
                or type(event.get("confirmed_after")) is not int
                or event["accepted"] != accepted
                or event["stock_after"] != remaining
                or event["confirmed_after"] != confirmed
            ):
                return "Commit outcome is inconsistent with the synthetic fixture"

    expected_state = {
        "initial_stock": FIXTURE["initial_stock"],
        "remaining_stock": remaining,
        "confirmed_count": confirmed,
    }
    if witness.get("final_state") != expected_state:
        return "Final state disagrees with the independently replayed history"
    if type(witness.get("complete")) is not bool:
        return "Completion status must be a boolean"
    computed = check_invariant(expected_state, complete=witness["complete"])
    if witness.get("verdict") != computed:
        return "Verdict disagrees with the independent oracle"
    return None


def validate_contract(witness: dict) -> str | None:
    """A digest is necessary but insufficient: validate the entire fixed history."""
    if not isinstance(witness, dict):
        return "Expected a JSON object"
    oracle = witness.get("oracle")
    variant, schedule = witness.get("variant"), witness.get("schedule")
    if not isinstance(oracle, dict):
        return "Missing immutable oracle binding"
    if (
        witness.get("execution_backend", "memory")
        not in ("memory", "native-postgresql")
        or witness.get("scope") != "SYNTHETIC_ONLY"
        or witness.get("target_id") != TARGET_ID
        or witness.get("target_revision") != TARGET_REVISION
        or witness.get("fixture") != FIXTURE
        or witness.get("fixture_hash") != FIXTURE_HASH
        or oracle != {
            "id": ORACLE_ID,
            "version": ORACLE_VERSION,
            "sha256": ORACLE_HASH,
            "authority": "fixed-demo-rule",
        }
        or type(variant) is not str
        or variant not in ("racy", "locked")
        or type(schedule) is not str
        or schedule not in SCHEDULES
    ):
        return "Witness does not match the fixed demo contracts"
    try:
        actual_digest = evidence_digest(witness)
    except (TypeError, ValueError, RecursionError):
        return "Witness cannot be serialized"
    if actual_digest != witness.get("evidence_sha256"):
        return "Evidence digest differs from the exported record"
    return _history_error(witness)


async def verify(
    witness: dict,
    target_factory: Callable[[], ContextManager[TargetLease]] | None = None,
) -> dict:
    """Three states: INVALID, REPRODUCED, or UNCONFIRMED."""
    rejected = validate_contract(witness)
    if rejected:
        return {"status": "INVALID", "reason": rejected}
    if not witness.get("complete"):
        return {"status": "UNCONFIRMED", "reason": "Original controlled run was incomplete"}

    if witness.get("execution_backend") == "native-postgresql":
        import os
        from uuid import uuid4

        admin_url = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
        control_url = os.getenv("CLASHBOLT_DATABASE_URL")
        if not admin_url or not control_url:
            return {
                "status": "UNCONFIRMED",
                "reason": "Independent native target database is not configured",
            }
        try:
            target_context = (
                target_factory() if target_factory
                else disposable_target(admin_url, control_url, uuid4())
            )
            with target_context as target:
                if service_enabled():
                    replay = await asyncio.to_thread(
                        request_fixture, witness["variant"], witness["schedule"], target.url
                    )
                else:
                    replay = await execute_postgres(
                        witness["variant"], witness["schedule"], target.url
                    )
        except (OSError, ValueError, RuntimeError, psycopg.Error):
            return {"status": "UNCONFIRMED", "reason": "Native target replay failed"}
    elif service_enabled():
        try:
            replay = await asyncio.to_thread(
                request_fixture, witness["variant"], witness["schedule"], None
            )
        except (OSError, ValueError):
            return {"status": "UNCONFIRMED", "reason": "Target service replay failed"}
    else:
        replay = await execute(witness["variant"], witness["schedule"])
    if validate_contract(replay) is not None:
        return {"status": "UNCONFIRMED", "reason": "Invalid evidence from replay"}
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
            "Fresh isolated fixture replayed the declared application checkpoints"
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
