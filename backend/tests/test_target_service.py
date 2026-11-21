import asyncio
import json
import os
import subprocess
import sys

import pytest
from clashbolt.target_service import MAX_REQUEST, run_request
from clashbolt.witness import validate_contract
from clashbolt.worker import isolated_synthetic_run


def test_fixed_service_protocol_executes_and_reduces_fixture():
    wire = {"variant": "locked", "schedule": "overlap"}
    child = subprocess.run(
        [sys.executable, "-m", "clashbolt.target_service"],
        input=json.dumps(wire).encode(), capture_output=True, check=True, timeout=5,
    )
    result = json.loads(child.stdout)
    assert result["verdict"] == "PASS"
    assert validate_contract(result) is None
    wire["gates"] = ["b.commit"]
    assert run_request(json.dumps(wire).encode()) == {"verdict": "UNKNOWN"}


@pytest.mark.parametrize("extra", [
    {"url": "postgresql://localhost/control"}, {"program": "anything"},
    {"gates": ["a.read", "a.read"]}, {"gates": ["a.commit", "a.read"]},
    {"gates": ["other"]}, {"login": {"instance": "../", "password": "x" * 43}},
    {"login": {"instance": "a" * 32, "password": "short"}},
])
def test_fixed_service_rejects_invalid_requests(extra):
    with pytest.raises(ValueError):
        run_request(json.dumps({"variant": "locked", "schedule": "overlap", **extra}).encode())


def test_service_limits_input_and_does_not_echo_credentials():
    with pytest.raises(ValueError, match="Oversized"):
        run_request(b"x" * (MAX_REQUEST + 1))
    child = subprocess.run(
        [sys.executable, "-m", "clashbolt.target_service"],
        input=b'{"password":"must-remain-private"}', capture_output=True, timeout=5,
    )
    assert child.returncode != 0
    assert child.stdout == child.stderr == b""


@pytest.mark.skipif(os.name != "posix", reason="Requires Unix domain sockets")
def test_configured_service_is_required_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "systemd")
    monkeypatch.setattr("clashbolt.target_service.SOCKET_PATH", str(tmp_path / "absent"))
    with pytest.raises(OSError):
        asyncio.run(isolated_synthetic_run("locked", "overlap"))
