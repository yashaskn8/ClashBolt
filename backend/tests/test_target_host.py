"""Opt-in host acceptance. Install the actual target units on a disposable host."""

import asyncio
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from clashbolt.reducer import reduce_witness
from clashbolt.target_db import disposable_target
from clashbolt.target_service import SOCKET_PATH
from clashbolt.witness import verify
from clashbolt.worker import isolated_synthetic_run

pytestmark = pytest.mark.skipif(
    os.getenv("CLASHBOLT_TARGET_TRANSPORT") != "systemd",
    reason="Requires installed and active systemd fixture socket",
)


def test_native_execution_and_verification_in_deployed_target(monkeypatch):
    admin = os.environ["CLASHBOLT_TEST_TARGET_ADMIN_URL"]
    control = os.environ["CLASHBOLT_TEST_DATABASE_URL"]
    monkeypatch.setenv("CLASHBOLT_TARGET_ADMIN_URL", admin)
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", control)
    with disposable_target(admin, control, uuid4()) as target:
        witness = asyncio.run(isolated_synthetic_run("racy", "overlap", target.url))
    assert witness["verdict"] == "FAIL"
    assert asyncio.run(verify(witness))["status"] == "REPRODUCED"
    assert reduce_witness(witness, admin, control)["status"] == "MINIMAL"
    assert asyncio.run(isolated_synthetic_run("locked", "overlap"))["verdict"] == "PASS"


def test_live_instances_have_distinct_uids_and_kernel_resource_limits():
    from time import monotonic, sleep

    clients = []
    try:
        for _ in range(2):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            clients.append(client)
            client.connect(SOCKET_PATH)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            units = subprocess.check_output([
                "systemctl", "list-units", "--state=running", "--no-legend",
                "--plain", "clashbolt-target@*.service",
            ], text=True)
            groups = [line.split()[0] for line in units.splitlines() if line.strip()]
            if len(groups) == 2:
                break
            sleep(0.1)
        assert len(groups) == 2
        uids = set()
        network_namespaces = set()
        mount_namespaces = set()
        host_network = os.readlink("/proc/self/ns/net")
        host_mount = os.readlink("/proc/self/ns/mnt")
        for unit in groups:
            path = subprocess.check_output([
                "systemctl", "show", "-p", "ControlGroup", "--value", unit,
            ], text=True).strip()
            assert path.startswith("/system.slice/")
            group = Path("/sys/fs/cgroup") / path.lstrip("/")
            assert int((group / "memory.max").read_text()) == 384 * 1024**2
            assert int((group / "pids.max").read_text()) == 8
            quota, period = map(int, (group / "cpu.max").read_text().split())
            assert quota * 2 == period
            # MainPID can briefly point to the root-owned exec trampoline
            # before systemd has completed DynamicUser credential switching.
            # Only accept a running fixture PID *after* it is non-root and
            # present in the restricted cgroup. Never relax the UID invariant.
            fixture_uid = None
            startup_deadline = monotonic() + 5
            while monotonic() < startup_deadline:
                pid = int(subprocess.check_output([
                    "systemctl", "show", "-p", "MainPID", "--value", unit,
                ], text=True).strip())
                if pid > 0:
                    try:
                        uid = Path(f"/proc/{pid}").stat().st_uid
                        members = (group / "cgroup.procs").read_text().split()
                    except FileNotFoundError:
                        # systemd swapped a short-lived startup process.
                        sleep(0.05)
                        continue
                    if uid not in (0, os.getuid()) and str(pid) in members:
                        fixture_uid = uid
                        break
                sleep(0.05)
            assert fixture_uid is not None, (
                f"Fixture never became a distinct non-root cgroup process: {unit}"
            )
            uids.add(fixture_uid)
            # Inspect the live process, not only the unit text. The CI host
            # runner has passwordless sudo for reading another UID's /proc.
            status = subprocess.check_output([
                "sudo", "-n", "cat", f"/proc/{pid}/status",
            ], text=True)
            fields = dict(
                line.split(":", 1) for line in status.splitlines() if ":" in line
            )
            assert fields["NoNewPrivs"].strip() == "1"
            assert fields["Seccomp"].strip() == "2"
            assert int(fields["CapEff"].strip(), 16) == 0
            assert int(fields["CapBnd"].strip(), 16) == 0
            for namespace, destination, host in (
                ("net", network_namespaces, host_network),
                ("mnt", mount_namespaces, host_mount),
            ):
                actual = subprocess.check_output([
                    "sudo", "-n", "readlink", f"/proc/{pid}/ns/{namespace}",
                ], text=True).strip()
                assert actual != host, f"{unit} shares the host {namespace} namespace"
                destination.add(actual)
        assert len(uids) == 2 and not uids.intersection({0, os.getuid()})
        assert len(network_namespaces) == 2
        assert len(mount_namespaces) == 2
    finally:
        for client in clients:
            client.close()
