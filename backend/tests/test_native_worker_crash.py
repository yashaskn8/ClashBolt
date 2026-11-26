"""Real native worker SIGKILL at two bounded target lifecycle checkpoints.

This test uses only the shipped fixture and disposable CI PostgreSQL clusters.
A killed worker must leave an unconfirmed claim until fenced recovery, and
reconciliation must remove every orphan before a replacement can certify it.
"""

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from clashbolt.experiment import ORACLE_HASH
from clashbolt.policy import Policy
from clashbolt.store import Ledger
from clashbolt.target_db import drop_abandoned_target
from clashbolt.worker import process_one, reconcile_targets

CRASHING_WORKER = """
import asyncio
import os
import time
from pathlib import Path

from clashbolt.policy import Policy
from clashbolt.store import Ledger
from clashbolt.worker import process_one


def pause(instance_id):
    Path(os.environ["CLASHBOLT_CRASH_MARKER"]).write_text(str(instance_id))
    while True:
        time.sleep(0.1)


original_track = Ledger.track_target
original_mark = Ledger.mark_target


def track(self, run_id, generation, owner, instance_id, *args, **kwargs):
    accepted = original_track(self, run_id, generation, owner, instance_id, *args, **kwargs)
    if accepted and os.environ["CLASHBOLT_CRASH_STAGE"] == "PROVISIONING":
        pause(instance_id)
    return accepted


def mark(self, instance_id, status, *args, **kwargs):
    original_mark(self, instance_id, status, *args, **kwargs)
    if status == "ACTIVE" and os.environ["CLASHBOLT_CRASH_STAGE"] == "ACTIVE":
        pause(instance_id)


Ledger.track_target = track
Ledger.mark_target = mark
asyncio.run(process_one(
    Policy(os.environ["CLASHBOLT_TEST_DATABASE_URL"]), "killed-native-worker"
))
"""


def _format_child_stderr(path: Path, *secrets_to_redact: str) -> str:
    if not path.is_file():
        return "<no child stderr captured>"
    text = path.read_text(encoding="utf-8", errors="replace")[-4000:]
    for secret in secrets_to_redact:
        if secret:
            text = text.replace(secret, "<redacted-dsn>")
    return text.strip() or "<empty child stderr>"


def test_crashing_worker_mark_wrapper_forwards_latency_kwargs(monkeypatch):
    parsed = ast.parse(CRASHING_WORKER)
    selected = [
        node for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name in ("track", "mark")
    ]
    calls: list[tuple[str, tuple, dict]] = []

    def stub_track(self, run_id, generation, owner, instance_id, *args, **kwargs):
        calls.append(("track", (self, run_id, generation, owner, instance_id, *args), kwargs))
        return True

    def stub_mark(self, instance_id, status, *args, **kwargs):
        calls.append(("mark", (self, instance_id, status, *args), kwargs))

    namespace = {
        "os": os,
        "original_track": stub_track,
        "original_mark": stub_mark,
        "pause": lambda instance_id: None,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "CRASHING_WORKER", "exec"), namespace)
    monkeypatch.setenv("CLASHBOLT_CRASH_STAGE", "NONE")

    stub_self = object()
    instance_id = uuid4()
    assert namespace["track"](stub_self, uuid4(), 1, "worker-a", instance_id) is True
    namespace["mark"](stub_self, instance_id, "ACTIVE", provision_ms=12.5, teardown_ms=4.25)

    assert calls[1] == (
        "mark",
        (stub_self, instance_id, "ACTIVE"),
        {"provision_ms": 12.5, "teardown_ms": 4.25},
    )
    mark_params = inspect.signature(Ledger.mark_target).parameters
    assert {"provision_ms", "teardown_ms"} <= set(mark_params)


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX SIGKILL")
@pytest.mark.parametrize("crash_stage", ("PROVISIONING", "ACTIVE"))
def test_native_worker_crash_reclaims_target_and_fences_stale_receipt(
    crash_stage, tmp_path, monkeypatch,
):
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not admin or not control:
        pytest.skip("Requires separate disposable control and target clusters")

    ledger = Policy(control)
    ledger.migrate()
    with ledger.connect() as conn:
        assert conn.info.dbname == "clashbolt_test"
        conn.execute(
            "TRUNCATE tool_audit, agent_plan, tool_capability, oracle_proposal, "
            "run_transition, experiment_run, project_quota CASCADE"
        )
    proposal = ledger.propose("worker-crash-boundary")
    approved = ledger.approve(UUID(proposal["id"]), ORACLE_HASH, "reviewer")
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    run = ledger.admit(
        client_key=str(uuid4()), variant="locked", schedule="serial",
        execution_backend="native-postgresql",
        approved_oracle_id=UUID(approved["id"]),
    )
    run_id = UUID(run["run_id"])
    marker = tmp_path / "native-crash-ready"
    stderr_log = tmp_path / "worker.stderr.log"
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    stderr_file = stderr_log.open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", CRASHING_WORKER],
        env={
            **os.environ,
            "CLASHBOLT_TEST_DATABASE_URL": control,
            "CLASHBOLT_CRASH_MARKER": str(marker),
            "CLASHBOLT_CRASH_STAGE": crash_stage,
        },
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.is_file() and time.monotonic() < deadline:
            if child.poll() is not None:
                stderr_file.flush()
                pytest.fail(
                    "Worker exited before the selected target crash point "
                    f"(rc={child.returncode}):\n"
                    f"{_format_child_stderr(stderr_log, control, admin)}"
                )
            time.sleep(0.05)
        assert marker.is_file(), (
            "Worker did not reach its target crash point:\n"
            f"{_format_child_stderr(stderr_log, control, admin)}"
        )
        instance_id = UUID(marker.read_text())

        with ledger.connect() as conn:
            target = conn.execute(
                "SELECT status, lease_generation FROM target_instance "
                "WHERE id = %s AND run_id = %s",
                (instance_id, run_id),
            ).fetchone()
            claimed = conn.execute(
                "SELECT status, lease_owner, lease_generation FROM experiment_run "
                "WHERE id = %s", (run_id,),
            ).fetchone()
        assert target and target["status"] == crash_stage
        assert claimed["status"] == "RUNNING"
        assert claimed["lease_owner"] == "killed-native-worker"
        assert claimed["lease_generation"] == target["lease_generation"]
        with psycopg.connect(admin, autocommit=True) as db:
            assert db.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{instance_id.hex}",),
            ).fetchone()[0] == (1 if crash_stage == "ACTIVE" else 0)

        child.kill()
        assert child.wait(timeout=5) != 0
        assert ledger.get(run_id)["verdict"] is None

        with ledger.connect() as conn:
            conn.execute(
                "UPDATE experiment_run "
                "SET lease_until = now() - interval '1 second' WHERE id = %s",
                (run_id,),
            )
            conn.execute(
                "UPDATE target_instance SET updated_at = "
                "now() - interval '10 seconds' WHERE id = %s",
                (instance_id,),
            )
        assert reconcile_targets(ledger) == 1
        assert ledger.target_instances(run_id)[0]["status"] == "CLEANED"
        with psycopg.connect(admin, autocommit=True) as db:
            assert db.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (f"cb_target_{instance_id.hex}",),
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = %s",
                (f"cb_runner_{instance_id.hex}",),
            ).fetchone()[0] == 0

        assert not ledger.finish(
            run_id, "killed-native-worker", claimed["lease_generation"], {},
        )
        assert asyncio.run(process_one(ledger, "replacement-native-worker"))
        completed = ledger.get(run_id)
        assert completed["status"] == "COMPLETE"
        assert completed["verdict"] == "PASS"
        assert completed["lease_generation"] == claimed["lease_generation"] + 1
        assert [event["status"] for event in ledger.transitions(run_id)] == [
            "PENDING", "RUNNING", "RUNNING", "COMPLETE",
        ]
        assert not ledger.finish(
            run_id, "killed-native-worker", claimed["lease_generation"], {},
        )
        assert all(
            item["status"] == "CLEANED" for item in ledger.target_instances(run_id)
        )
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        stderr_file.close()
        for target in ledger.target_instances(run_id):
            with suppress(psycopg.Error):
                drop_abandoned_target(admin, control, UUID(target["id"]))
