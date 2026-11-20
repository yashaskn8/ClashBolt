"""A deterministic two-client race in a disposable, in-memory fixture.

No external targets, databases, subprocesses, or arbitrary user code are executed.
The checkpoints control application actions, not OS or PostgreSQL scheduling.
"""

import asyncio
import json

import psycopg
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

Variant = Literal["racy", "locked"]
Schedule = Literal["overlap", "serial"]
Verdict = Literal["PASS", "FAIL", "UNKNOWN"]

TARGET_ID = "synthetic-inventory"
TARGET_REVISION = "inventory-demo-v1"
ORACLE_ID = "confirmed-orders-not-above-stock"
ORACLE_VERSION = "1"
ORACLE_RULE = (
    "confirmed_count <= initial_stock and "
    "remaining_stock == initial_stock - confirmed_count"
)
ORACLE_HASH = sha256(ORACLE_RULE.encode()).hexdigest()
FIXTURE = {"initial_stock": 1, "clients": ["a", "b"], "quantity_each": 1}
FIXTURE_HASH = sha256(json.dumps(FIXTURE, sort_keys=True).encode()).hexdigest()

SCHEDULES: dict[Schedule, tuple[str, ...]] = {
    "overlap": ("a.read", "b.read", "a.commit", "b.commit"),
    "serial": ("a.read", "a.commit", "b.read", "b.commit"),
}


@dataclass
class InventoryFixture:
    """Fresh state is constructed for every experiment or replay."""

    stock: int = 1
    confirmed_count: int = 0

    def final_state(self) -> dict:
        return {
            "initial_stock": FIXTURE["initial_stock"],
            "remaining_stock": self.stock,
            "confirmed_count": self.confirmed_count,
        }

    def read(self, client: str) -> dict:
        return {"client": client, "checkpoint": "read", "observed_stock": self.stock}

    def commit(self, client: str, observed: int, variant: Variant) -> dict:
        # A realistic lost-check business bug: both clients may have read stock=1.
        # The corrected path rechecks the *current* state before accepting.
        available = observed if variant == "racy" else self.stock
        accepted = available >= 1
        if accepted:
            self.stock = available - 1
            self.confirmed_count += 1
        return {
            "client": client,
            "checkpoint": "commit",
            "accepted": accepted,
            "stock_after": self.stock,
            "confirmed_after": self.confirmed_count,
        }


class PostgresInventoryFixture:
    """Restricted role runs real SQL on a fresh disposable database."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def read(self, client: str) -> dict:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            row = conn.execute("SELECT remaining FROM inventory WHERE id = 1").fetchone()
            if row is None:
                raise RuntimeError("Disposable fixture missing")
            return {"client": client, "checkpoint": "read", "observed_stock": row[0]}

    def commit(self, client: str, observed: int, variant: Variant) -> dict:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT remaining FROM inventory WHERE id = 1 FOR UPDATE"
            ).fetchone()
            if row is None:
                raise RuntimeError("Disposable fixture missing")
            available = observed if variant == "racy" else row[0]
            accepted = available >= 1
            if accepted:
                conn.execute(
                    """
                    UPDATE inventory SET remaining = %s,
                        confirmed_count = confirmed_count + 1 WHERE id = 1
                    """,
                    (available - 1,),
                )
            new = conn.execute(
                "SELECT remaining, confirmed_count FROM inventory WHERE id = 1"
            ).fetchone()
            return {
                "client": client,
                "checkpoint": "commit",
                "accepted": accepted,
                "stock_after": new[0],
                "confirmed_after": new[1],
            }

    def final_state(self) -> dict:
        with psycopg.connect(self.dsn, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT remaining, confirmed_count FROM inventory WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Disposable fixture missing")
            return {
                "initial_stock": FIXTURE["initial_stock"],
                "remaining_stock": row[0],
                "confirmed_count": row[1],
            }


@dataclass
class CheckpointController:
    schedule: tuple[str, ...]
    position: int = 0
    observed: list[dict] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def step(self, label: str, action) -> dict:
        async with self.condition:
            await self.condition.wait_for(
                lambda: self.position < len(self.schedule)
                and self.schedule[self.position] == label
            )
            # A synchronous fixture action completes before the next gate opens.
            event = {"sequence": self.position + 1, **action()}
            self.observed.append(event)
            self.position += 1
            self.condition.notify_all()
            return event


def check_invariant(state: dict | None, *, complete: bool) -> Verdict:
    """A fixed independent demo oracle. Incomplete evidence is never PASS/FAIL."""
    if not complete or not state:
        return "UNKNOWN"
    if not all(k in state for k in ("initial_stock", "remaining_stock", "confirmed_count")):
        return "UNKNOWN"
    initial, remaining, confirmed = (
        state["initial_stock"],
        state["remaining_stock"],
        state["confirmed_count"],
    )
    if not all(type(value) is int for value in (initial, remaining, confirmed)):
        return "UNKNOWN"
    if min(initial, remaining, confirmed) < 0:
        return "FAIL"
    return "PASS" if confirmed <= initial and remaining == initial - confirmed else "FAIL"


async def execute(variant: Variant, schedule: Schedule) -> dict:
    """Run the shipped in-memory fixture."""
    return await _execute(variant, schedule, InventoryFixture(), "memory")


async def execute_postgres(variant: Variant, schedule: Schedule, dsn: str) -> dict:
    """Run the same fixed gates against a freshly provisioned PostgreSQL target."""
    return await _execute(
        variant, schedule, PostgresInventoryFixture(dsn), "native-postgresql"
    )


async def _execute(variant: Variant, schedule: Schedule, fixture, backend: str) -> dict:
    if variant not in ("racy", "locked") or schedule not in SCHEDULES:
        raise ValueError("Only shipped synthetic targets and schedules are permitted")
    controller = CheckpointController(SCHEDULES[schedule])

    async def client(client_id: str) -> None:
        read = await controller.step(f"{client_id}.read", lambda: fixture.read(client_id))
        await controller.step(
            f"{client_id}.commit",
            lambda: fixture.commit(client_id, read["observed_stock"], variant),
        )

    complete = False
    try:
        async with asyncio.timeout(2.0):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(client("a"))
                tasks.create_task(client("b"))
        complete = True
    except TimeoutError:
        # A stalled gate is evidence of an incomplete experiment, not a bug.
        pass

    state = fixture.final_state()
    verdict = check_invariant(state, complete=complete)
    evidence = {
        "target_id": TARGET_ID,
        "target_revision": TARGET_REVISION,
        "variant": variant,
        "fixture": FIXTURE,
        "fixture_hash": FIXTURE_HASH,
        "schedule": schedule,
        "requested_gates": list(controller.schedule),
        "observed_steps": controller.observed,
        "oracle": {
            "id": ORACLE_ID,
            "version": ORACLE_VERSION,
            "sha256": ORACLE_HASH,
            "authority": "fixed-demo-rule",
        },
        "final_state": state,
        "complete": complete,
        "verdict": verdict,
    }
    if backend != "memory":
        evidence["execution_backend"] = backend
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    digest = sha256(encoded).hexdigest()
    return {**evidence, "evidence_sha256": digest, "scope": "SYNTHETIC_ONLY"}
