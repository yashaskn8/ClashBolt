"""One fixed fixture request per systemd-accepted connection, with bounded I/O."""

import asyncio
import json
import os
import re
import socket
import sys
from time import monotonic
from typing import Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from clashbolt.experiment import SCHEDULES, Schedule, Variant, execute, execute_postgres
from clashbolt.fixture_runner import _apply_fixed_resource_limits

SOCKET_PATH = "/run/clashbolt-target.sock"
MAX_REQUEST = 2048
MAX_RESPONSE = 100_000


class TargetLogin(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    instance: str = Field(pattern="^[0-9a-f]{32}$")
    password: SecretStr = Field(min_length=43, max_length=43)


class FixtureRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    variant: Variant
    schedule: Schedule
    gates: list[Literal["a.read", "b.read", "a.commit", "b.commit"]] | None = Field(
        default=None, max_length=4
    )
    login: TargetLogin | None = None


def service_enabled() -> bool:
    mode = os.environ.get("CLASHBOLT_TARGET_TRANSPORT", "subprocess")
    if mode not in ("systemd", "subprocess"):
        raise ValueError("Unknown fixed target transport")
    return mode == "systemd"


def request_fixture(
    variant: str, schedule: str, target_url: str | None, gates: list[str] | None = None,
) -> dict:
    """The root-owned socket accepts worker-group clients only."""
    payload = {"variant": variant, "schedule": schedule, "gates": gates}
    if target_url is not None:
        settings = conninfo_to_dict(target_url)
        match = re.fullmatch(r"cb_target_([a-f0-9]{32})", settings["dbname"])
        if (
            not match or settings["user"] != "cb_runner_" + match[1]
            or settings.get("port") != "5433"
        ):
            raise ValueError("Target does not match the deployed fixture cluster")
        payload["login"] = {"instance": match[1], "password": settings["password"]}
    wire = json.dumps(payload).encode()
    if len(wire) > MAX_REQUEST:
        raise ValueError("Oversized fixture request")
    deadline = monotonic() + 8
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(8)
        client.connect(SOCKET_PATH)
        client.sendall(wire)
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        while len(response) <= MAX_RESPONSE:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Target service timed out")
            client.settimeout(remaining)
            chunk = client.recv(8192)
            if not chunk:
                break
            response.extend(chunk)
    if not response or len(response) > MAX_RESPONSE:
        raise ValueError("Invalid target service response")
    result = json.loads(response)
    if not isinstance(result, dict):
        raise ValueError("Expected a target result object")
    return result


def run_request(raw: bytes) -> dict:
    if len(raw) > MAX_REQUEST:
        raise ValueError("Oversized fixture request")
    request = FixtureRequest.model_validate_json(raw)
    dsn = None
    if request.login is not None:
        dsn = make_conninfo(
            host="/run/clashbolt-db", port=5433,
            dbname="cb_target_" + request.login.instance,
            user="cb_runner_" + request.login.instance,
            password=request.login.password.get_secret_value(),
            require_auth="scram-sha-256", connect_timeout=2,
        )
    if request.gates is not None:
        from clashbolt.reducer import _probe

        selected = [gate for gate in SCHEDULES[request.schedule] if gate in request.gates]
        if selected != request.gates:
            raise ValueError("Reduction must preserve the original gate order")
        return {"verdict": _probe(request.variant, selected, dsn)}
    if dsn is None:
        return asyncio.run(execute(request.variant, request.schedule))
    return asyncio.run(execute_postgres(request.variant, request.schedule, dsn))


def main() -> int:
    _apply_fixed_resource_limits()
    try:
        result = run_request(sys.stdin.buffer.read(MAX_REQUEST + 1))
        sys.stdout.buffer.write(json.dumps(result).encode())
    except (OSError, ValueError, RuntimeError, psycopg.Error, ExceptionGroup):
        # Even validation errors can contain input. Never echo them to the journal.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
