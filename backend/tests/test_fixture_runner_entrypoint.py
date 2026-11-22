"""Exercise trusted fixed fixture runner entrypoints in coverage process.

The subprocess protocol is still exercised independently by integration tests;
these tests inspect bounded argument and resource-limit branches directly.
"""

import json
import os
import sys

import pytest
from clashbolt import fixture_runner


@pytest.mark.skipif(os.name != "posix", reason="POSIX resource limits only")
def test_fixed_runner_limits_cap_unbounded_and_preexisting_smaller_limits(monkeypatch):
    import resource

    current = {
        resource.RLIMIT_CPU: (2, 10),
        resource.RLIMIT_AS: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        resource.RLIMIT_FSIZE: (4096, 8192),
        resource.RLIMIT_NOFILE: (resource.RLIM_INFINITY, 16),
    }
    applied = {}
    monkeypatch.setattr(resource, "getrlimit", lambda kind: current[kind])
    monkeypatch.setattr(
        resource, "setrlimit",
        lambda kind, value: applied.setdefault(kind, value),
    )
    fixture_runner._apply_fixed_resource_limits()
    assert applied == {
        resource.RLIMIT_CPU: (2, 2),
        resource.RLIMIT_AS: (768 * 1024 * 1024, 768 * 1024 * 1024),
        resource.RLIMIT_FSIZE: (4096, 4096),
        resource.RLIMIT_NOFILE: (16, 16),
    }


@pytest.mark.parametrize("native", (False, True))
def test_fixture_runner_entrypoint_selects_only_approved_fixture(
    monkeypatch, capsys, native,
):
    calls = []

    async def memory(variant, schedule):
        calls.append(("memory", variant, schedule))
        return {"verdict": "PASS", "scope": "SYNTHETIC_ONLY"}

    async def postgres(variant, schedule, target_url):
        calls.append(("native", variant, schedule, target_url))
        return {"verdict": "PASS", "scope": "SYNTHETIC_ONLY"}

    monkeypatch.setattr(fixture_runner, "execute", memory)
    monkeypatch.setattr(fixture_runner, "execute_postgres", postgres)
    monkeypatch.setattr(fixture_runner, "_apply_fixed_resource_limits", lambda: None)
    monkeypatch.setattr(sys, "argv", ["fixture_runner", "locked", "serial"])
    if native:
        monkeypatch.setenv("CLASHBOLT_TARGET_DATABASE_URL", "fixed-fixture-url")
    else:
        monkeypatch.delenv("CLASHBOLT_TARGET_DATABASE_URL", raising=False)

    fixture_runner.main()
    assert calls == [
        ("native", "locked", "serial", "fixed-fixture-url")
        if native else ("memory", "locked", "serial")
    ]
    assert json.loads(capsys.readouterr().out) == {
        "verdict": "PASS", "scope": "SYNTHETIC_ONLY",
    }


@pytest.mark.parametrize("args", [
    ["arbitrary", "serial"], ["locked", "arbitrary"], ["locked"], [],
])
def test_fixture_runner_rejects_unrecognized_target_parameters(
    monkeypatch, args,
):
    monkeypatch.setattr(sys, "argv", ["fixture_runner", *args])
    with pytest.raises(SystemExit) as result:
        fixture_runner.main()
    assert result.value.code == 2
