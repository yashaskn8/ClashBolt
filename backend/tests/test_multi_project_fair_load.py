"""Bounded fair-load trial over real independent PostgreSQL project schemas.

This is a fixed synthetic queue-drain regression, not an external workload
benchmark or a production fairness/service-level guarantee.
"""

import asyncio
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from clashbolt.accounts import AccountStore
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.worker import process_one


def test_three_project_rotating_worker_drains_concurrent_backlogs(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable native PostgreSQL test database required")
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    registry = AccountStore(dsn)
    registry.migrate()
    projects = []
    for index in range(3):
        project_id = f"fairload-{uuid4().hex[:12]}"
        schema = registry.create_project(project_id)
        ledger = Policy(dsn, schema=schema, project_id=project_id)
        proposal = ledger.propose(f"fairload-{index}")
        approval = ledger.approve(
            UUID(proposal["id"]), ORACLE_HASH, "independent-fixture-reviewer"
        )
        with ledger.connect() as conn:
            conn.execute(
                "UPDATE project_quota SET max_open = 12 WHERE project_id = 'local'"
            )
        projects.append((ledger, UUID(approval["id"])))
    known_schemas = {
        (p["project_id"], p["schema_name"]) for p in registry.worker_projects()
    }
    assert all((p.project_id, p.schema) in known_schemas for p, _ in projects)

    # Simultaneous arrivals across all projects use independent connections
    # and leave durable queued records before any worker starts.
    tasks = [(index, number) for number in range(10) for index in range(3)]

    def admit(task):
        index, _ = task
        ledger, approved = projects[index]
        return ledger.admit(
            client_key=str(uuid4()), variant="locked", schedule="serial",
            approved_oracle_id=approved,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(admit, tasks))
    assert len({run["run_id"] for run in created}) == 30
    assert all(run["status"] == "PENDING" for run in created)

    # The production worker rotates its start project and processes at most
    # one execution per cycle. Exercise that same policy with the real
    # process_one entrypoint and freshly spawned fixed fixture children.
    served = []
    for cycle in range(30):
        for offset in range(len(projects)):
            index = (cycle + offset) % len(projects)
            ledger, _ = projects[index]
            if asyncio.run(process_one(ledger, f"fairload-worker-{cycle}")):
                served.append(index)
                break
        else:
            pytest.fail("A runnable project was starved or queue capacity was lost")

    assert len(served) == 30
    assert Counter(served) == Counter({0: 10, 1: 10, 2: 10})
    # Under equal fixed backlogs, no project waits more than two cycles.
    assert served == [cycle % 3 for cycle in range(30)]
    for index, (ledger, _) in enumerate(projects):
        expected = created[index::3]
        assert len(expected) == 10
        for run in expected:
            run_id = UUID(run["run_id"])
            result = ledger.get(run_id)
            assert result["status"] == "COMPLETE"
            assert result["verdict"] == "PASS"
            assert [e["status"] for e in ledger.transitions(run_id)] == [
                "PENDING", "RUNNING", "COMPLETE",
            ]
        assert ledger.claim(f"fairload-empty-{index}") is None
