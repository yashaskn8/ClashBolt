"""Fail-closed audit of the shipped native target deployment and secrets.

This examines trusted repository files only. It is not a substitute for the
separate native-host acceptance tests or an external deployment audit.
"""

import re
import sys
from pathlib import Path


UNITS = {
    "clashbolt-api.service": {
        "Service": {
            "User": "clashbolt-api",
            "Group": "clashbolt-api",
            "NoNewPrivileges": "yes",
            "CapabilityBoundingSet": "",
            "PrivateTmp": "yes",
            "PrivateDevices": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectControlGroups": "yes",
            "RestrictSUIDSGID": "yes",
            "LockPersonality": "yes",
            "RestrictRealtime": "yes",
            "IPAddressDeny": "any",
            "IPAddressAllow": "localhost",
            "UMask": "0077",
            "MemoryMax": "1G",
            "TasksMax": "128",
            "KillMode": "control-group",
        },
    },
    "clashbolt-worker.service": {
        "Service": {
            "User": "clashbolt-worker",
            "Group": "clashbolt-worker",
            "NoNewPrivileges": "yes",
            "CapabilityBoundingSet": "",
            "PrivateTmp": "yes",
            "PrivateDevices": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectControlGroups": "yes",
            "RestrictSUIDSGID": "yes",
            "LockPersonality": "yes",
            "RestrictRealtime": "yes",
            "IPAddressDeny": "any",
            "IPAddressAllow": "localhost",
            "UMask": "0077",
            "MemoryMax": "1G",
            "TasksMax": "128",
            "KillMode": "control-group",
        },
    },
    "clashbolt-target@.service": {
        "Service": {
            "DynamicUser": "yes",
            "User": "cb-%i",
            "RootDirectory": "/var/lib/clashbolt-target-root",
            "MountAPIVFS": "no",
            "NoNewPrivileges": "yes",
            "CapabilityBoundingSet": "",
            "PrivateNetwork": "yes",
            "RestrictAddressFamilies": "AF_UNIX",
            "PrivateDevices": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "InaccessiblePaths": "-/proc -/sys",
            "RestrictNamespaces": "yes",
            "RestrictSUIDSGID": "yes",
            "LockPersonality": "yes",
            "SystemCallArchitectures": "native",
            "UMask": "0077",
            "MemoryMax": "384M",
            "MemorySwapMax": "0",
            "CPUQuota": "50%",
            "TasksMax": "8",
            "LimitNOFILE": "64",
            "LimitCORE": "0",
            "LimitCPU": "3",
            "RuntimeMaxSec": "8s",
            "KillMode": "control-group",
            "StandardError": "null",
        },
    },
    "clashbolt-target.socket": {
        "Socket": {
            "ListenStream": "/run/clashbolt-target.sock",
            "Accept": "yes",
            "SocketUser": "root",
            "SocketGroup": "clashbolt-worker",
            "SocketMode": "0660",
            "MaxConnections": "8",
            "RemoveOnStop": "yes",
        },
    },
}

PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH) PRIVATE KEY-----")
CREDENTIALS = (
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{70,}\b"),
)


def _parse_unit(source: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current in sections:
                raise ValueError(f"Repeated unit section: {current}")
            sections[current] = {}
            continue
        if current is None or "=" not in line:
            raise ValueError("Malformed systemd directive")
        key, value = line.split("=", 1)
        if key in sections[current] and key != "BindReadOnlyPaths":
            raise ValueError(f"Repeated security-relevant directive: {key}")
        # Repeated BindReadOnlyPaths entries are additive, unlike overrides.
        sections[current][key] = value
    return sections


def audit_units(files: dict[str, str]) -> None:
    if set(files) != set(UNITS):
        raise ValueError("Missing or unexpected native deployment units")
    for name, required in UNITS.items():
        sections = _parse_unit(files[name])
        for section, values in required.items():
            actual = sections.get(section, {})
            for key, expected in values.items():
                if actual.get(key) != expected:
                    raise ValueError(f"Deployment hardening changed: {name}:{key}")
        if name.endswith(".service"):
            command = sections["Service"].get("ExecStart", "")
            if not command.startswith(("/opt/clashbolt/", "/opt/clashbolt-fixture/", "/usr/bin/env ")):
                raise ValueError(f"Service has unexpected execution entrypoint: {name}")
            if any(token in command for token in ("/bin/sh", "/bin/bash", " -c ", "sudo ")):
                raise ValueError("Shell or privilege escalation in service entrypoint")
    target = _parse_unit(files["clashbolt-target@.service"])["Service"]
    if (
        "SystemCallFilter" not in target
        or "@system-service" not in target["SystemCallFilter"]
        or "io_uring_setup" not in target["SystemCallFilter"]
        or "BindReadOnlyPaths" not in target
    ):
        raise ValueError("Missing isolated target syscall or socket mount boundary")
    for name in ("clashbolt-api.service", "clashbolt-worker.service"):
        if "--host 127.0.0.1" not in _parse_unit(files[name])["Service"]["ExecStart"] and name == "clashbolt-api.service":
            raise ValueError("API may only bind loopback")


def audit_credentials(paths: dict[str, str]) -> None:
    for name, content in paths.items():
        for line_no, line in enumerate(content.splitlines(), 1):
            if PRIVATE_KEY.search(line) or any(pattern.search(line) for pattern in CREDENTIALS):
                raise ValueError(f"Potential committed credential: {name}:{line_no}")


def main() -> int:
    directory = Path("deploy/systemd")
    units = {name: (directory / name).read_text(encoding="utf-8") for name in UNITS}
    audit_units(units)
    production = {
        str(path): path.read_text(encoding="utf-8")
        for root in (
            Path("backend/clashbolt"),
            Path("frontend/src"),
            Path("deploy/systemd"),
            Path(".github/workflows"),
        )
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".tsx", ".ts", ".service", ".socket", ".yml"}
    }
    audit_credentials(production)
    print("Native service isolation directives and production credential patterns verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
