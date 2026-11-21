"""CI-only PostgreSQL crash recovery drill; never targets a user database.

The control cluster is stopped and restarted by the containing GitHub Actions
job between the two modes. This tests a single recovered primary, not HA failover.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy


def run(stage: str, evidence_path: Path) -> None:
    if os.getenv("CI") != "true":
        raise RuntimeError("Crash drill is restricted to ephemeral CI runners")
    dsn = os.environ["CLASHBOLT_TEST_DATABASE_URL"]
    store = Policy(dsn)
    with store.connect() as conn:
        if (
            conn.info.dbname != "clashbolt_test"
            or conn.info.host not in ("127.0.0.1", "::1", "localhost")
        ):
            raise RuntimeError("Unexpected CI control database target")
    if stage == "prepare":
        store.migrate()
        # Only the private disposable CI database may be reset for this drill.
        with store.connect() as conn:
            conn.execute(
                "TRUNCATE tool_audit, agent_plan, tool_capability, "
                "oracle_proposal, run_transition, experiment_run, "
                "project_quota CASCADE"
            )
        proposal = store.propose("control-crash-acknowledged")
        approved = store.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
        submitted = store.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=UUID(approved["id"]),
        )
        original = store.claim("control-crash-original", lease_seconds=30)
        if not original or original["run_id"] != submitted["run_id"]:
            raise AssertionError("Durable claim not acknowledged")
        evidence_path.write_text(json.dumps({
            "run_id": submitted["run_id"],
            "generation": original["lease_generation"],
        }), encoding="utf-8")
        return

    receipt = json.loads(evidence_path.read_text(encoding="utf-8"))
    run_id = UUID(receipt["run_id"])
    persisted = store.get(run_id)
    if not persisted or persisted["status"] != "RUNNING":
        raise AssertionError("Acknowledged run disappeared after primary crash")
    events = store.transitions(run_id)
    if [event["status"] for event in events] != ["PENDING", "RUNNING"]:
        raise AssertionError("Accepted transition history was lost after crash")
    if persisted["lease_generation"] != receipt["generation"]:
        raise AssertionError("Lease generation changed during recovery")

    # Advance lease time in the test database instead of sleeping 30 seconds.
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE experiment_run SET lease_until = now() - interval '1 second'
            WHERE id = %s
            """,
            (run_id,),
        )
    replacement = store.claim("control-crash-replacement", lease_seconds=30)
    if not replacement or replacement["run_id"] != str(run_id):
        raise AssertionError("Pending recovery claim was not discoverable")
    if replacement["lease_generation"] != receipt["generation"] + 1:
        raise AssertionError("Recovery did not fence the previous worker")
    witness = asyncio.run(execute("locked", "serial"))
    if store.finish(run_id, "control-crash-original", receipt["generation"], witness):
        raise AssertionError("Stale worker accepted after primary restart")
    if not store.finish(
        run_id, "control-crash-replacement",
        replacement["lease_generation"], witness
    ):
        raise AssertionError("Replacement worker could not finalize recovered run")
    if store.finish(
        run_id, "control-crash-replacement",
        replacement["lease_generation"], witness
    ):
        raise AssertionError("Duplicate terminal receipt was accepted")
    if [r["status"] for r in store.transitions(run_id)] != [
        "PENDING", "RUNNING", "RUNNING", "COMPLETE",
    ]:
        raise AssertionError("Recovered transition history is inconsistent")
    if store.get(run_id)["verdict"] != "PASS":
        raise AssertionError("Recovered run has no certified synthetic verdict")


def main() -> None:
    parser = argparse.ArgumentParser(description="Disposable CI control restart drill")
    parser.add_argument("stage", choices=("prepare", "verify"))
    parser.add_argument("evidence_path", type=Path)
    args = parser.parse_args()
    run(args.stage, args.evidence_path)


if __name__ == "__main__":
    main()
