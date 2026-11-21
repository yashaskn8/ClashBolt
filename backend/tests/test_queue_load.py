"""Bounded real-PostgreSQL admission/claim load checks, not synthetic SLO proof."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from clashbolt.experiment import ORACLE_HASH, execute
from clashbolt.policy import Policy


def test_concurrent_admission_enforces_quota_and_drains_without_starvation():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL integration cluster required")
    ledger = Policy(dsn)
    ledger.migrate()
    with ledger.connect() as conn:
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
        conn.execute(
            "INSERT INTO project_quota(project_id, max_open) VALUES ('local', 24)"
        )
    proposal = ledger.propose("bounded-backlog-test")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    approval = UUID(approved["id"])

    def admit(_):
        try:
            return ledger.admit(
                client_key=str(uuid4()), variant="locked", schedule="serial",
                approved_oracle_id=approval,
            )
        except ValueError as error:
            assert "quota" in str(error).lower()
            return None

    # Admission must be serialized against the durable quota even when the
    # callers arrive concurrently with distinct idempotency keys.
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(pool.map(admit, range(36)))
    accepted = [run for run in admissions if run]
    assert len(accepted) == 24
    assert len({run["run_id"] for run in accepted}) == 24

    # Real independent DB connections compete for queue claims. No job is
    # delivered to two current owners in a healthy primary.
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(
            lambda i: ledger.claim(f"queue-load-worker-{i}"), range(36)
        ))
    claimed = [run for run in claims if run]
    assert len(claimed) == 24
    assert {run["run_id"] for run in claimed} == {
        run["run_id"] for run in accepted
    }

    witness = asyncio.run(execute("locked", "serial"))

    def finish(claim):
        return ledger.finish(
            UUID(claim["run_id"]), claim["lease_owner"],
            claim["lease_generation"], witness,
        )

    # Ledger.claim deliberately omits the private owner field from public run
    # projections; the claiming worker ID is the original request argument.
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(
            lambda index: ledger.finish(
                UUID(claimed[index]["run_id"]),
                f"queue-load-worker-{claims.index(claimed[index])}",
                claimed[index]["lease_generation"],
                witness,
            ),
            range(len(claimed)),
        ))
    assert all(outcomes)
    assert all(ledger.get(UUID(row["run_id"]))["status"] == "COMPLETE"
               for row in accepted)
    assert all(len(ledger.transitions(UUID(row["run_id"]))) == 3
               for row in accepted)
    # After finalization, capacity is reusable without restarting a worker.
    another = ledger.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        approved_oracle_id=approval,
    )
    assert another["status"] == "PENDING"
    assert ledger.operations()["runs"]["PENDING"] == 1
