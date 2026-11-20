import asyncio
import json
import os
import subprocess
import sys

import pytest
from clashbolt.worker import isolated_synthetic_run


def test_fixed_child_receives_no_control_secrets_or_repository_cwd(monkeypatch):
    monkeypatch.setenv("CLASHBOLT_OPERATOR_TOKEN", "must-not-reach-child")
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "must-not-reach-child")
    monkeypatch.setenv("HOME", "must-not-reach-child")
    captured = {}
    original = asyncio.create_subprocess_exec

    async def inspected_spawn(*args, **kwargs):
        captured.update(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", inspected_spawn)
    witness = asyncio.run(isolated_synthetic_run("locked", "serial"))
    assert witness["verdict"] == "PASS"
    assert captured["env"].get("CLASHBOLT_OPERATOR_TOKEN") is None
    assert captured["env"].get("CLASHBOLT_DATABASE_URL") is None
    assert captured["env"].get("HOME") is None
    assert captured["cwd"] != os.getcwd()
    assert not os.path.exists(captured["cwd"])
    assert captured["start_new_session"] is (os.name == "posix")


def test_posix_fixed_runner_sets_cpu_file_and_descriptor_limits():
    if os.name != "posix":
        return
    program = (
        "import json, resource; "
        "from clashbolt.fixture_runner import _apply_fixed_resource_limits; "
        "_apply_fixed_resource_limits(); "
        "print(json.dumps([resource.getrlimit(x)[0] for x in "
        "(resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE)]))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=10, check=True,
    )
    cpu, file_size, files = json.loads(finished.stdout)
    assert cpu <= 3
    assert file_size <= 1024 * 1024
    assert files <= 64


def test_oversized_child_stdout_is_stopped_before_full_capture(monkeypatch):
    original = asyncio.create_subprocess_exec

    async def oversized_spawn(*args, **kwargs):
        return await original(
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'X' * 262144)",
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", oversized_spawn)
    with pytest.raises(ValueError, match="output exceeded"):
        asyncio.run(isolated_synthetic_run("locked", "serial"))
