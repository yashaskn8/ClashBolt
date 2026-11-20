"""PostgreSQL control-plane ledger for local synthetic experiments.

All state transitions, queue claims, and events commit in the same transaction.
This module deliberately does not connect to or execute an external target.
"""

import hashlib
import json
import os
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from clashbolt.experiment import ORACLE_HASH, SCHEDULES, TARGET_REVISION
from clashbolt.target_db import validate_target_cluster
from clashbolt.witness import validate_contract

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS project_quota (
        project_id TEXT PRIMARY KEY,
        max_open INTEGER NOT NULL DEFAULT 20 CHECK (max_open BETWEEN 1 AND 1000)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiment_run (
        id UUID PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project_quota(project_id),
        client_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        variant TEXT NOT NULL CHECK (variant IN ('racy', 'locked')),
        schedule TEXT NOT NULL CHECK (schedule IN ('overlap', 'serial')),
        oracle_hash TEXT NOT NULL,
        step_budget INTEGER NOT NULL CHECK (step_budget BETWEEN 4 AND 32),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'RUNNING', 'CANCELLING',
                             'CANCELLED', 'COMPLETE', 'UNKNOWN')),
        version BIGINT NOT NULL DEFAULT 1,
        lease_generation BIGINT NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_until TIMESTAMPTZ,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        witness JSONB,
        verdict TEXT CHECK (verdict IN ('PASS', 'FAIL', 'UNKNOWN')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, client_key)
    )
    """,
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "consumed_steps INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "execution_backend TEXT NOT NULL DEFAULT 'memory' "
    "CHECK (execution_backend IN ('memory', 'native-postgresql'))",
    """
    CREATE INDEX IF NOT EXISTS experiment_claim_idx
    ON experiment_run (status, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS run_transition (
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        version BIGINT NOT NULL,
        status TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, version)
    )
    """,
)


def _request_hash(
    variant: str, schedule: str, budget: int, approval_id: UUID, execution_backend: str
) -> str:
    request = {
        "variant": variant,
        "schedule": schedule,
        "step_budget": budget,
        "oracle_hash": ORACLE_HASH,
        "approved_oracle_id": str(approval_id),
    }
    if execution_backend != "memory":
        request["execution_backend"] = execution_backend
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def _public(row: dict) -> dict:
    """Return JSON-safe, explicit fields rather than exposing internal lease secrets."""
    return {
        "run_id": str(row["id"]),
        "project_id": row["project_id"],
        "variant": row["variant"],
        "schedule": row["schedule"],
        "oracle_hash": row["oracle_hash"],
        "approved_oracle_id": (
            str(row["approved_oracle_id"]) if row["approved_oracle_id"] else None
        ),
        "step_budget": row["step_budget"],
        "consumed_steps": row["consumed_steps"],
        "execution_backend": row["execution_backend"],
        "status": row["status"],
        "version": row["version"],
        "lease_generation": row["lease_generation"],
        "cancel_requested": row["cancel_requested"],
        "verdict": row["verdict"],
        "witness": row["witness"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _event(conn: psycopg.Connection, row: dict, note: str) -> None:
    conn.execute(
        """
        INSERT INTO run_transition(run_id, version, status, note)
        VALUES (%s, %s, %s, %s)
        """,
        (row["id"], row["version"], row["status"], note),
    )


class Ledger:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)

    def migrate(self) -> None:
        with self.connect() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)

    def admit(
        self,
        *,
        client_key: str,
        variant: str,
        schedule: str,
        step_budget: int = 4,
        approved_oracle_id: UUID | None = None,
        execution_backend: str = "memory",
    ) -> dict:
        """A retry with the same key+request returns the same run; conflicts reject."""
        if variant not in ("racy", "locked") or schedule not in SCHEDULES:
            raise ValueError("Unknown built-in fixture variant or schedule")
        if not 4 <= step_budget <= 32:
            raise ValueError("Step budget must be between 4 and 32")
        if not 1 <= len(client_key) <= 100:
            raise ValueError("Client key must be 1-100 characters")

        if not isinstance(approved_oracle_id, UUID):
            raise ValueError("Durable runs require an approved oracle version")
        if execution_backend not in ("memory", "native-postgresql"):
            raise ValueError("Unsupported execution backend")
        if execution_backend == "native-postgresql":
            target_admin = os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
            if not target_admin:
                raise ValueError("Separate native target cluster is not configured")
            validate_target_cluster(target_admin, self.dsn)
        request_hash = _request_hash(
            variant, schedule, step_budget, approved_oracle_id, execution_backend
        )
        with self.connect() as conn:
            # The quota row serializes admission for this local workspace.
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            conn.execute(
                "SELECT max_open FROM project_quota WHERE project_id = 'local' FOR UPDATE"
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM experiment_run WHERE project_id = 'local' AND client_key = %s",
                (client_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("Idempotency key reused for a different experiment")
                return _public(existing)

            approval = conn.execute(
                """
                SELECT state, rule_hash, target_revision, reviewed_by
                FROM oracle_proposal WHERE id = %s FOR SHARE
                """,
                (approved_oracle_id,),
            ).fetchone()
            if (
                not approval or approval["state"] != "APPROVED"
                or approval["rule_hash"] != ORACLE_HASH
                or approval["target_revision"] != TARGET_REVISION
                or not approval["reviewed_by"]
            ):
                raise ValueError("Durable admission requires a current approved oracle")
            count = conn.execute(
                """
                SELECT count(*) AS open_count
                FROM experiment_run
                WHERE project_id = 'local'
                  AND status IN ('PENDING', 'RUNNING', 'CANCELLING')
                """
            ).fetchone()["open_count"]
            limit = conn.execute(
                "SELECT max_open FROM project_quota WHERE project_id = 'local'"
            ).fetchone()["max_open"]
            if count >= limit:
                raise ValueError("Active experiment quota exhausted")
            row = conn.execute(
                """
                INSERT INTO experiment_run (
                    id, project_id, client_key, request_hash,
                    variant, schedule, oracle_hash, step_budget, approved_oracle_id,
                    execution_backend
                )
                VALUES (%s, 'local', %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    uuid4(), client_key, request_hash, variant, schedule,
                    ORACLE_HASH, step_budget, approved_oracle_id, execution_backend,
                ),
            ).fetchone()
            _event(conn, row, "admitted")
            return _public(row)

    def recent(self, limit: int = 20) -> list[dict]:
        """Read-only recent-run index for recovery after UI/browser restarts."""
        if not 1 <= limit <= 100:
            raise ValueError("Recent-run limit must be 1-100")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM experiment_run WHERE project_id = 'local'
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [_public(row) for row in rows]

    def get(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s AND project_id = 'local'",
                (run_id,),
            ).fetchone()
            return _public(row) if row else None

    def transitions(self, run_id: UUID, after: int = 0) -> list[dict]:
        with self.connect() as conn:
            events = conn.execute(
                """
                SELECT version, status, note, created_at
                FROM run_transition WHERE run_id = %s AND version > %s
                ORDER BY version ASC LIMIT 100
                """,
                (run_id, after),
            ).fetchall()
            return [
                {**event, "created_at": event["created_at"].isoformat()}
                for event in events
            ]

    def claim(self, owner: str, lease_seconds: int = 30) -> dict | None:
        if not owner or len(owner) > 100 or not 5 <= lease_seconds <= 300:
            raise ValueError("Invalid worker or lease duration")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM experiment_run
                WHERE project_id = 'local'
                  AND (
                    status = 'PENDING'
                    OR (status = 'RUNNING' AND lease_until < now()
                        AND cancel_requested = FALSE)
                  )
                ORDER BY created_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if not row:
                return None
            claimed = conn.execute(
                """
                UPDATE experiment_run SET
                    status = 'RUNNING',
                    version = version + 1,
                    lease_generation = lease_generation + 1,
                    lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (owner, lease_seconds, row["id"]),
            ).fetchone()
            _event(conn, claimed, "worker claimed run")
            return _public(claimed)

    def finish(self, run_id: UUID, owner: str, generation: int, witness: dict) -> bool:
        """A stale worker cannot publish a result after its lease is lost."""
        with self.connect() as conn:
            # Consistent lock order: oracle approval first, run row second.
            # A simultaneous revoke cannot race with accepted certification.
            binding = conn.execute(
                "SELECT approved_oracle_id FROM experiment_run WHERE id = %s",
                (run_id,),
            ).fetchone()
            approval = None
            if binding and binding["approved_oracle_id"]:
                approval = conn.execute(
                    """
                    SELECT state, rule_hash, target_revision
                    FROM oracle_proposal WHERE id = %s FOR SHARE
                    """,
                    (binding["approved_oracle_id"],),
                ).fetchone()
            current = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if (
                not current
                or current["approved_oracle_id"] != (
                    binding["approved_oracle_id"] if binding else None
                )
                or current["lease_owner"] != owner
                or current["lease_generation"] != generation
                or current["lease_until"] is None
                or current["lease_until"] <= conn.execute("SELECT now() AS now").fetchone()["now"]
                or current["status"] not in ("RUNNING", "CANCELLING")
            ):
                return False

            observed = witness.get("observed_steps")
            consumed = len(observed) if isinstance(observed, list) else 0
            if current["cancel_requested"]:
                status, verdict, note = "UNKNOWN", "UNKNOWN", "cancelled during execution"
            elif consumed > current["step_budget"]:
                status, verdict, note = "UNKNOWN", "UNKNOWN", "step budget exceeded"
            elif (
                not approval
                or approval["state"] != "APPROVED"
                or approval["rule_hash"] != current["oracle_hash"]
                or approval["target_revision"] != TARGET_REVISION
            ):
                status, verdict, note = "UNKNOWN", "UNKNOWN", "oracle approval withdrawn"
            elif (
                witness.get("oracle", {}).get("sha256") != current["oracle_hash"]
                or witness.get("variant") != current["variant"]
                or witness.get("schedule") != current["schedule"]
                or witness.get("execution_backend", "memory")
                    != current["execution_backend"]
                or validate_contract(witness) is not None
            ):
                status, verdict, note = "UNKNOWN", "UNKNOWN", "invalid or mismatched witness"
            else:
                verdict = witness.get("verdict")
                if verdict not in ("PASS", "FAIL") or not witness.get("complete"):
                    status, verdict, note = "UNKNOWN", "UNKNOWN", "incomplete evidence"
                else:
                    status, note = "COMPLETE", "independent demo oracle evaluated"
            updated = conn.execute(
                """
                UPDATE experiment_run SET status = %s, verdict = %s,
                    witness = %s, consumed_steps = %s,
                    version = version + 1, lease_owner = NULL,
                    lease_until = NULL, updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (status, verdict, Jsonb(witness), consumed, run_id),
            ).fetchone()
            _event(conn, updated, note)
            return True

    def cancel(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s AND project_id = 'local' FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] in ("PENDING", "RUNNING"):
                status = "CANCELLED" if row["status"] == "PENDING" else "CANCELLING"
                row = conn.execute(
                    """
                    UPDATE experiment_run SET
                        cancel_requested = TRUE, status = %s,
                        version = version + 1, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (status, run_id),
                ).fetchone()
                _event(conn, row, "operator requested cancellation")
            return _public(row)

    def reconcile_expired_cancelled(self) -> int:
        """An expired in-flight cancellation is UNKNOWN, never a clean cancel."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM experiment_run
                WHERE status = 'CANCELLING' AND lease_until < now()
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for item in rows:
                updated = conn.execute(
                    """
                    UPDATE experiment_run SET status = 'UNKNOWN', verdict = 'UNKNOWN',
                        version = version + 1, lease_owner = NULL, lease_until = NULL,
                        updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (item["id"],),
                ).fetchone()
                _event(conn, updated, "cancelled claim expired; effect ambiguous")
            return len(rows)

if __name__ == "__main__":
    import os

    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        raise SystemExit("Set CLASHBOLT_DATABASE_URL to a local PostgreSQL database")
    Ledger(dsn).migrate()
    print("ClashBolt PostgreSQL schema ready")
