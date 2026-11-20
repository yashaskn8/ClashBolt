"""Transactionally persisted agent decisions; hashes detect accidental tampering.

The caller holds the agent_plan row lock when appending events to a known plan.
This is a tamper-evident log against ordinary SQL mutation, not a cryptographic
defense against the PostgreSQL administrator or filesystem modification.
"""

import hashlib
import json
from uuid import UUID

from psycopg.types.json import Jsonb

VERSION = "agent-decision-history-v1"
GENESIS = "0" * 64
EVENT_TYPES = frozenset({
    "PROPOSED", "DISPATCHED", "RECEIPT", "TIMEOUT", "ORACLE_REVOKED",
    "FEEDBACK",
})


def _digest(plan_id: UUID | str, sequence: int, event: str, payload: dict,
            previous_sha256: str) -> str:
    data = {
        "version": VERSION,
        "plan_id": str(plan_id),
        "sequence": sequence,
        "event": event,
        "payload": payload,
        "previous_sha256": previous_sha256,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append_history(conn, plan_id: UUID, event: str, payload: dict) -> None:
    """Write an event in the same transaction as the associated state change."""
    if event not in EVENT_TYPES or not isinstance(payload, dict):
        raise ValueError("Unsupported agent history entry")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 16_384:
        raise ValueError("Agent history payload exceeds the fixed size limit")
    previous = conn.execute(
        """
        SELECT sequence, entry_sha256 FROM agent_history
        WHERE plan_id = %s ORDER BY sequence DESC LIMIT 1
        """,
        (plan_id,),
    ).fetchone()
    sequence = previous["sequence"] + 1 if previous else 1
    prior_hash = previous["entry_sha256"] if previous else GENESIS
    digest = _digest(plan_id, sequence, event, payload, prior_hash)
    conn.execute(
        """
        INSERT INTO agent_history (
            plan_id, sequence, event, payload, previous_sha256, entry_sha256
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (plan_id, sequence, event, Jsonb(payload), prior_hash, digest),
    )


def verify_history(plan_id: UUID, entries: list[dict]) -> dict:
    """Verify every link and contiguous sequence; no history is unconfirmed."""
    if not entries:
        return {"status": "UNCONFIRMED", "reason": "No recorded agent history"}
    previous = GENESIS
    for sequence, entry in enumerate(entries, 1):
        if (
            entry.get("sequence") != sequence
            or entry.get("event") not in EVENT_TYPES
            or entry.get("previous_sha256") != previous
            or entry.get("entry_sha256") != _digest(
                plan_id, sequence, entry["event"], entry["payload"], previous
            )
        ):
            return {"status": "INVALID", "reason": "Decision history integrity failure"}
        previous = entry["entry_sha256"]
    if entries[0]["event"] != "PROPOSED":
        return {"status": "INVALID", "reason": "No initial proposal in history"}
    return {"status": "VERIFIED", "head_sha256": previous, "entries": len(entries)}
