"""Mutation tests for native systemd isolation and secret-leak policy."""

import errno
import os
import runpy
import socket
from pathlib import Path

import pytest

from ci.check_deployment_security import UNITS, audit_credentials, audit_units


def _units():
    root = Path("deploy/systemd")
    return {name: (root / name).read_text(encoding="utf-8") for name in UNITS}


def test_native_units_pass_checked_in_security_contract():
    audit_units(_units())


@pytest.mark.parametrize(
    "file,section,key,value",
    [
        (file, section, key, value)
        for file, sections in UNITS.items()
        for section, items in sections.items()
        for key, value in items.items()
    ],
)
def test_mutating_any_required_native_isolation_setting_blocks_ci(
    file, section, key, value,
):
    units = _units()
    needle = f"{key}={value}"
    assert needle in units[file], f"Missing tested setting in {file}: {needle}"
    units[file] = units[file].replace(needle, f"{key}=DISABLED", 1)
    with pytest.raises(ValueError, match="Deployment hardening changed"):
        audit_units(units)


def test_missing_unit_and_duplicate_security_override_block_ci():
    units = _units()
    del units["clashbolt-target.socket"]
    with pytest.raises(ValueError, match="Missing"):
        audit_units(units)

    units = _units()
    units["clashbolt-target@.service"] += "\nPrivateNetwork=no\n"
    with pytest.raises(ValueError, match="Repeated"):
        audit_units(units)


@pytest.mark.parametrize("mutation", [
    "remove_syscall_filter",
    "remove_socket_mount",
    "run_target_shell",
    "bind_api_publicly",
    "remove_all_socket",
])
def test_unsafe_native_entrypoints_and_socket_changes_block_ci(mutation):
    units = _units()
    target = "clashbolt-target@.service"
    if mutation == "remove_syscall_filter":
        units[target] = units[target].replace(
            "SystemCallFilter=~@debug @mount @privileged io_uring_setup", ""
        )
    elif mutation == "remove_socket_mount":
        units[target] = "\n".join(
            line for line in units[target].splitlines() if not line.startswith("BindReadOnlyPaths=")
        )
    elif mutation == "run_target_shell":
        units[target] = units[target].replace(
            "ExecStart=/opt/clashbolt-fixture/bin/python",
            "ExecStart=/bin/sh -c /opt/clashbolt-fixture/bin/python",
        )
    elif mutation == "bind_api_publicly":
        units["clashbolt-api.service"] = units["clashbolt-api.service"].replace(
            "--host 127.0.0.1", "--host 0.0.0.0"
        )
    elif mutation == "remove_all_socket":
        del units["clashbolt-target.socket"]
    with pytest.raises(ValueError):
        audit_units(units)


@pytest.mark.parametrize("secret", [
    "AKIA" + "A" * 16,
    "ghp_" + "A" * 36,
    "github_pat_" + "a" * 70,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_embedded_credentials_block_ci_without_printing_the_value(secret):
    with pytest.raises(ValueError, match="Potential committed credential") as exc:
        audit_credentials({"backend/clashbolt/security.py": f"credential = '{secret}'"})
    assert secret not in str(exc.value)


def test_ordinary_fixed_fixture_markers_not_mistaken_for_secrets():
    audit_credentials({
        "backend/clashbolt/synthetic.py": (
            "fixture_hash = '0' * 64\n"
            "test_url = 'postgresql://127.0.0.1:5433/postgres'\n"
            "scope = 'SYNTHETIC_ONLY'\n"
        )
    })


@pytest.mark.parametrize("ip_errno", [errno.EPERM, errno.EAFNOSUPPORT, errno.EACCES, None])
def test_native_preflight_requires_kernel_ip_denial(monkeypatch, ip_errno):
    resource = pytest.importorskip("resource")
    calls = []
    closed = []
    original_exists = Path.exists

    class Probe:
        def close(self):
            closed.append(True)

    def socket_probe(family, kind):
        calls.append((family, kind))
        if ip_errno is not None:
            raise OSError(ip_errno, "simulated native isolation")
        return Probe()

    hidden = {
        "/etc/clashbolt-host-canary", "/run/clashbolt-target.sock",
        "/run/postgresql/.s.PGSQL.5432", "/proc/1/environ",
    }

    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 1000)
        patch.setattr(resource, "getrlimit", lambda key: (
            (64, 64) if key == resource.RLIMIT_NOFILE else (3, 3)
        ))
        patch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)
        patch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
        patch.setattr(Path, "is_socket", lambda p: str(p) == "/run/clashbolt-db/.s.PGSQL.5433")
        patch.setattr(Path, "exists", lambda p: False if str(p) in hidden else original_exists(p))
        patch.setattr(socket, "socket", socket_probe)
        if ip_errno is None:
            with pytest.raises(AssertionError, match="could open an IP socket"):
                runpy.run_path("ci/fixture_preflight.py")
        else:
            runpy.run_path("ci/fixture_preflight.py")

    assert calls[0] == (socket.AF_INET, socket.SOCK_STREAM)
    if ip_errno is None:
        assert closed == [True]
    else:
        assert calls[1:] == [(socket.AF_INET6, socket.SOCK_STREAM)]
        assert closed == []
