"""A transient PostgreSQL deadlock must not permanently stop the worker loop."""

import asyncio
import sys

import psycopg
import pytest
from clashbolt import worker


class _FakePolicy:
    def __init__(self, *_args, **_kwargs):
        pass

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


def test_worker_continues_after_one_deadlocked_verification_claim(monkeypatch):
    _stub_worker(monkeypatch)
    calls = []

    class NextCycleObserved(Exception):
        pass

    async def verification(_ledger, _owner):
        calls.append("verification")
        if len(calls) == 1:
            raise psycopg.errors.DeadlockDetected("injected transaction deadlock")
        raise NextCycleObserved

    monkeypatch.setattr(worker, "process_verification_one", verification)
    with pytest.raises(NextCycleObserved):
        asyncio.run(worker.main())
    assert len(calls) == 2


@pytest.mark.parametrize("once,attempts", [(False, 3), (True, 1)])
def test_worker_repeated_deadlocks_fail_closed(monkeypatch, once, attempts):
    _stub_worker(monkeypatch, once=once)
    calls = []

    async def verification(_ledger, _owner):
        calls.append("verification")
        raise psycopg.errors.DeadlockDetected("injected transaction deadlock")

    monkeypatch.setattr(worker, "process_verification_one", verification)
    with pytest.raises(RuntimeError, match="Worker database deadlock persisted"):
        asyncio.run(worker.main())
    assert len(calls) == attempts
