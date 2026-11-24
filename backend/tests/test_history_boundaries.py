"""A malformed decision record must never verify or disrupt the integrity sweep."""

from uuid import UUID

import pytest
from clashbolt.agent_history import GENESIS, _digest, append_history, verify_history

PLAN = UUID(int=1)


def entry(event="PROPOSED", payload=None):
    payload = {} if payload is None else payload
    return {
        "sequence": 1, "event": event, "payload": payload, "previous_sha256": GENESIS,
        "entry_sha256": _digest(PLAN, 1, event, payload, GENESIS),
    }


@pytest.mark.parametrize("changes", [
    {"sequence": True}, {"sequence": 1.0}, {"sequence": 2}, {"event": []},
    {"event": "OTHER"}, {"payload": []}, {"previous_sha256": "0"}, {"entry_sha256": "0"},
])
def test_invalid_link_is_rejected(changes):
    assert verify_history(PLAN, [entry() | changes])["status"] == "INVALID"


def test_missing_nonobject_and_unrooted_history():
    assert verify_history(PLAN, [])["status"] == "UNCONFIRMED"
    assert verify_history(PLAN, [None])["status"] == "INVALID"
    assert verify_history(PLAN, [entry("RECEIPT")])["status"] == "INVALID"
    assert verify_history(PLAN, [entry()])["status"] == "VERIFIED"


def test_nonfinite_legacy_payload_cannot_verify():
    broken = entry()
    broken["payload"] = {"score": float("nan")}
    assert verify_history(PLAN, [broken])["status"] == "INVALID"


@pytest.mark.parametrize("event,payload", [
    ("UNSUPPORTED", {}), ("PROPOSED", []), ("FEEDBACK", {"note": "x" * 16_384}),
    ("RECEIPT", {"value": float("nan")}),
])
def test_invalid_append_fails_before_database_access(event, payload):
    with pytest.raises(ValueError):
        append_history(None, PLAN, event, payload)
