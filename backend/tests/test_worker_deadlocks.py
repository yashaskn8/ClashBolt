"""Bounded worker recovery after PostgreSQL transaction and connection failures."""

import asyncio
import sys

import psycopg
import pytest
from clashbolt import worker


class _FakePolicy:
    def __init__(self, *_args, project_id="local", **_kwargs):
        self.project_id = project_id

    def heartbeat(self, _owner):
        pass

    def reconcile_corrupt_plans(self):
        pass

    def reconcile_stalled_plans(self):
        pass

    def reconcile_stalled_model_calls(self):
        pass


def _stub_worker(monkeypatch, *, once=False):
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "postgresql://fake/unused")
    monkeypatch.delenv("CLASHBOLT_ACCESS_MODE", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["clashbolt.worker", "--interval", "0.1", *(["--once"] if once else [])],
    )
    monkeypatch.setattr(worker, "Policy", _FakePolicy)

    async def no_run(_ledger, _owner):
        return False

    monkeypatch.setattr(worker, "process_one", no_run)


@pytest.mark.parametrize("error_type", [psycopg.errors.DeadlockDetected, psycopg.OperationalError])
def test_worker_continues_after_one_transient_database_failure(monkeypatch, error_type):
    _stub_worker(monkeypatch)
    calls = []

    class NextCycleObserved(Exception):
        pass

    async def verification(_ledger, _owner):
        calls.append("verification")
        if len(calls) == 1:
            raise error_type("injected database failure")
        raise NextCycleObserved

    monkeypatch.setattr(worker, "process_verification_one", verification)
    with pytest.raises(NextCycleObserved):
        asyncio.run(worker.main())
    assert len(calls) == 2


@pytest.mark.parametrize("once,attempts", [(False, 3), (True, 1)])
@pytest.mark.parametrize("error_type,message", [
    (psycopg.errors.DeadlockDetected, "Worker database deadlock persisted"),
    (psycopg.OperationalError, "Worker database connection failed"),
])
def test_worker_repeated_database_failures_fail_closed(
    monkeypatch, once, attempts, error_type, message,
):
    _stub_worker(monkeypatch, once=once)
    calls = []

    async def verification(_ledger, _owner):
        calls.append("verification")
        raise error_type("injected database failure")

    monkeypatch.setattr(worker, "process_verification_one", verification)
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(worker.main())
    assert len(calls) == attempts


def test_worker_rotates_after_served_project_when_other_projects_are_idle(monkeypatch):
    _stub_worker(monkeypatch)
    monkeypatch.setenv("CLASHBOLT_ACCESS_MODE", "accounts")
    positions = ["local", "first", "second", "third"]

    class Registry:
        def worker_projects(self):
            return [{"project_id": name, "schema_name": name} for name in positions]

    monkeypatch.setattr(worker, "AccountStore", lambda _dsn: Registry())
    served = []

    class EndOfTrial(Exception):
        pass

    async def process(ledger, _owner):
        if len(served) == 6:
            raise EndOfTrial
        if ledger.project_id == "local":
            return False
        served.append(ledger.project_id)
        return True

    async def no_verification(_ledger, _owner):
        return False

    monkeypatch.setattr(worker, "process_one", process)
    monkeypatch.setattr(worker, "process_verification_one", no_verification)
    with pytest.raises(EndOfTrial):
        asyncio.run(worker.main())
    assert served == positions[1:] * 2
