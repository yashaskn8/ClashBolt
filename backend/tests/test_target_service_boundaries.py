"""Fail-closed tests for the fixed Unix-socket target protocol.

Socket responses are simulated in process. No arbitrary target command,
outside network endpoint or privileged deployment is contacted.
"""

import io
import json
from types import SimpleNamespace

import pytest
from clashbolt import target_service
from clashbolt.experiment import SCHEDULES
from clashbolt.witness import validate_contract
from psycopg.conninfo import conninfo_to_dict, make_conninfo

INSTANCE = "a" * 32
PASSWORD = "p" * 43
DATABASE = f"cb_target_{INSTANCE}"
ROLE = f"cb_runner_{INSTANCE}"


class SocketPeer:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.endpoint = None
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, endpoint):
        self.endpoint = endpoint

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        self.how = how

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


def _peer(monkeypatch, chunks):
    peer = SocketPeer(chunks)
    family = object()
    monkeypatch.setattr(target_service.socket, "AF_UNIX", family, raising=False)

    def connect(requested_family, kind):
        assert requested_family is family
        assert kind == target_service.socket.SOCK_STREAM
        return peer

    monkeypatch.setattr(target_service.socket, "socket", connect)
    return peer


def _dsn(**overrides):
    return make_conninfo(
        host=overrides.pop("host", "127.0.0.1"),
        port=overrides.pop("port", "5433"),
        dbname=overrides.pop("dbname", DATABASE),
        user=overrides.pop("user", ROLE),
        password=overrides.pop("password", PASSWORD),
        **overrides,
    )


def test_transport_selects_only_known_fixed_service_modes(monkeypatch):
    monkeypatch.delenv("CLASHBOLT_TARGET_TRANSPORT", raising=False)
    assert not target_service.service_enabled()
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "systemd")
    assert target_service.service_enabled()
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "subprocess")
    assert not target_service.service_enabled()
    monkeypatch.setenv("CLASHBOLT_TARGET_TRANSPORT", "arbitrary")
    with pytest.raises(ValueError, match="transport"):
        target_service.service_enabled()


def test_socket_success_sends_only_fixed_fields_and_reads_chunked_json(monkeypatch):
    peer = _peer(monkeypatch, [b'{"ver', b'dict":"PASS"}', b""])
    expected = {"verdict": "PASS"}
    assert target_service.request_fixture("locked", "serial", _dsn()) == expected
    assert peer.endpoint == target_service.SOCKET_PATH
    payload = json.loads(peer.sent)
    assert payload == {
        "variant": "locked", "schedule": "serial", "gates": None,
        "login": {"instance": INSTANCE, "password": PASSWORD},
    }
    assert peer.timeouts and all(0 < value <= 8 for value in peer.timeouts)


def test_unavailable_unix_socket_fails_without_falling_back(monkeypatch):
    monkeypatch.delattr(target_service.socket, "AF_UNIX", raising=False)

    def should_not_open(*_args):
        pytest.fail("The target transport must never fall back to another socket family")

    monkeypatch.setattr(target_service.socket, "socket", should_not_open)
    with pytest.raises(OSError, match="Unix socket support"):
        target_service.request_fixture("locked", "serial", None)


@pytest.mark.parametrize("invalid", [
    {"host": "198.51.100.12"},
    {"host": "127.0.0.1", "hostaddr": "198.51.100.12"},
    {"port": "5432"},
    {"user": "another_role"},
    {"dbname": "wrong_database"},
    {"dbname": ""},
    {"password": ""},
    {"service": "untrusted"},
])
def test_socket_refuses_untrusted_target_credentials(monkeypatch, invalid):
    # Reject before opening a socket, including remote-routing overrides.
    def should_not_open(*_args):
        pytest.fail("Untrusted endpoint reached the fixed target socket")

    monkeypatch.setattr(target_service.socket, "socket", should_not_open)
    with pytest.raises(ValueError, match="deployed fixture cluster") as exc:
        target_service.request_fixture("locked", "serial", _dsn(**invalid))
    assert PASSWORD not in str(exc.value)


@pytest.mark.parametrize("chunks,expected", [
    ([b""], "Invalid target service response"),
    ([b"a" * (target_service.MAX_RESPONSE + 1)], "Invalid target service response"),
    ([b'["not an object"]'], "Expected a target result object"),
    ([b"not-json"], "Expecting value"),
])
def test_socket_fails_closed_on_malformed_bounded_response(
    monkeypatch, chunks, expected,
):
    _peer(monkeypatch, chunks)
    with pytest.raises(ValueError, match=expected):
        target_service.request_fixture("locked", "serial", None)


def test_socket_checks_deadline_and_outgoing_message_size(monkeypatch):
    _peer(monkeypatch, [b'{"verdict":"PASS"}'])
    clock = iter((10.0, 19.0))
    monkeypatch.setattr(target_service, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="timed out"):
        target_service.request_fixture("locked", "serial", None)
    with pytest.raises(ValueError, match="Oversized fixture request"):
        target_service.request_fixture("x" * target_service.MAX_REQUEST, "serial", None)


def test_fixed_reduction_login_is_constructed_from_server_owned_fields(
    monkeypatch,
):
    evidence = {}

    def probe(variant, selected, dsn):
        evidence["variant"] = variant
        evidence["gates"] = selected
        evidence["dsn"] = conninfo_to_dict(dsn)
        return "FAIL"

    monkeypatch.setattr("clashbolt.reducer._probe", probe)
    chosen = [SCHEDULES["overlap"][0], SCHEDULES["overlap"][2]]
    wire = {
        "variant": "racy", "schedule": "overlap",
        "gates": chosen,
        "login": {"instance": INSTANCE, "password": PASSWORD},
    }
    assert target_service.run_request(json.dumps(wire).encode()) == {"verdict": "FAIL"}
    assert evidence["variant"] == "racy"
    assert evidence["gates"] == chosen
    config = evidence["dsn"]
    assert config["host"] == "/run/clashbolt-db"
    assert config["port"] == "5433"
    assert config["dbname"] == DATABASE
    assert config["user"] == ROLE
    assert config["password"] == PASSWORD
    assert config["require_auth"] == "scram-sha-256"


def test_service_native_execution_uses_only_validated_login(monkeypatch):
    async def execute_native(variant, schedule, dsn):
        config = conninfo_to_dict(dsn)
        assert config["user"] == ROLE
        assert config["dbname"] == DATABASE
        assert (variant, schedule) == ("locked", "serial")
        return {"verdict": "PASS"}

    monkeypatch.setattr(target_service, "execute_postgres", execute_native)
    wire = {
        "variant": "locked", "schedule": "serial",
        "login": {"instance": INSTANCE, "password": PASSWORD},
    }
    assert target_service.run_request(json.dumps(wire).encode()) == {
        "verdict": "PASS"
    }


def test_target_service_main_does_not_echo_invalid_requests(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(target_service, "_apply_fixed_resource_limits", lambda: None)
    monkeypatch.setattr(
        target_service.sys, "stdin",
        SimpleNamespace(buffer=io.BytesIO(b'{"password":"confidential"}')),
    )
    monkeypatch.setattr(
        target_service.sys, "stdout", SimpleNamespace(buffer=output),
    )
    assert target_service.main() == 1
    assert output.getvalue() == b""


def test_target_service_main_encodes_only_valid_result(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(target_service, "_apply_fixed_resource_limits", lambda: None)
    monkeypatch.setattr(
        target_service.sys, "stdin",
        SimpleNamespace(buffer=io.BytesIO(b'{"variant":"locked","schedule":"serial"}')),
    )
    monkeypatch.setattr(
        target_service.sys, "stdout", SimpleNamespace(buffer=output),
    )
    assert target_service.main() == 0
    result = json.loads(output.getvalue())
    assert result["verdict"] == "PASS"
    assert validate_contract(result) is None
