"""PostgreSQL control-plane ledger for local synthetic experiments.

All state transitions, queue claims, and events commit in the same transaction.
This module deliberately does not connect to or execute an external target.
"""

import hashlib
import json
import os
import re
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from clashbolt.experiment import ORACLE_HASH, SCHEDULES, TARGET_REVISION
from clashbolt.target_db import validate_target_cluster
from clashbolt.witness import validate_contract

SCHEMA_LOCK_ID = 538192047
GLOBAL_NATIVE_LOCK_ID = 538192048
GLOBAL_NATIVE_SLOTS = 2
BASE_SCHEMA_VERSION = 7
_PROJECT_SCHEMA_NAME = re.compile(r'(?:public|cbp_[0-9a-f]{24})\Z')

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS project_quota (
        project_id TEXT PRIMARY KEY,
        max_open INTEGER NOT NULL DEFAULT 20 CHECK (max_open BETWEEN 1 AND 1000)
    )
    """,
    "ALTER TABLE project_quota ADD COLUMN IF NOT EXISTS "
    "max_native_active INTEGER NOT NULL DEFAULT 2 "
    "CHECK (max_native_active BETWEEN 1 AND 16)",
    """
    CREATE TABLE IF NOT EXISTS experiment_draft (
        project_id TEXT PRIMARY KEY REFERENCES project_quota(project_id),
        version BIGINT NOT NULL DEFAULT 1,
        draft JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    CREATE TABLE IF NOT EXISTS target_instance (
        id UUID PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        lease_generation BIGINT NOT NULL,
        owner TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('PROVISIONING', 'ACTIVE', 'CLEANING', 'CLEANED', 'UNKNOWN')
        ),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, lease_generation)
    )
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
    """
    CREATE TABLE IF NOT EXISTS run_reduction (
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        run_version BIGINT NOT NULL,
        report JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (run_id, run_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_job (
        id UUID PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        run_version BIGINT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('REPLAY', 'REDUCE')),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETE', 'UNKNOWN')),
        lease_owner TEXT,
        lease_generation BIGINT NOT NULL DEFAULT 0,
        lease_until TIMESTAMPTZ,
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, run_version, kind)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS verification_job_claim_idx
    ON verification_job(status, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS witness_review (
        id UUID PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        run_version BIGINT NOT NULL,
        evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
        reviewer_account_id UUID NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('SUPPORTED', 'WITHHELD')),
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 12 AND 400),
        replay_job_id UUID REFERENCES verification_job(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(run_id, run_version, reviewer_account_id),
        CHECK (decision != 'SUPPORTED' OR replay_job_id IS NOT NULL)
    )
    """,
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_witness_review()
    RETURNS TRIGGER LANGUAGE plpgsql AS $review_guard$
    BEGIN
        RAISE EXCEPTION 'Witness review audit is append-only';
    END
    $review_guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_witness_review_immutable ON witness_review",
    """
    CREATE TRIGGER clashbolt_witness_review_immutable
    BEFORE UPDATE OR DELETE ON witness_review FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_witness_review()
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_target_instance (
        id UUID PRIMARY KEY,
        job_id UUID NOT NULL REFERENCES verification_job(id),
        lease_generation BIGINT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('PROVISIONING', 'ACTIVE', 'CLEANING', 'CLEANED', 'UNKNOWN')
        ),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS verification_target_cleanup_idx
    ON verification_target_instance (status, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_heartbeat (
        owner TEXT PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
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


def _public(row: dict, project_id: str = "local") -> dict:
    """Return JSON-safe, explicit fields rather than exposing internal lease secrets."""
    return {
        "run_id": str(row["id"]),
        "project_id": project_id,
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


def _record_schema_revision(conn, component: str, version: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_revision (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    current = conn.execute(
        "SELECT version FROM schema_revision WHERE component = %s FOR UPDATE",
        (component,),
    ).fetchone()
    if current and current["version"] > version:
        raise ValueError("Database schema is newer than the installed application")
    conn.execute(
        """
        INSERT INTO schema_revision(component, version)
        VALUES (%s, %s)
        ON CONFLICT (component)
        DO UPDATE SET version = EXCLUDED.version, updated_at = now()
        """,
        (component, version),
    )


class Ledger:
    def __init__(
        self, dsn: str, *, schema: str = "public", project_id: str = "local",
        actor_account_id: UUID | None = None, required_role: str = "viewer",
    ):
        # Only trusted project-registry schemas may be used for resources.
        # One instance holds one immutable scope, not a request-global context.
        if not _PROJECT_SCHEMA_NAME.fullmatch(schema):
            raise ValueError("Unrecognized project database schema")
        if (schema == "public") != (project_id == "local"):
            raise ValueError("Project identity and database schema disagree")
        if required_role not in ("viewer", "operator", "reviewer"):
            raise ValueError("Unrecognized required project role")
        if actor_account_id is not None and not isinstance(actor_account_id, UUID):
            raise ValueError("Project actor must be an authenticated UUID")
        self.dsn = dsn
        self.schema = schema
        self.project_id = project_id
        self.actor_account_id = actor_account_id
        self.required_role = required_role

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)
        try:
            # Never fall back to public from an isolated project: a missing
            # project table must error, not silently expose another workspace.
            conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            if self.actor_account_id is not None:
                # Database row locks span the entire operation. A concurrent
                # grant revocation waits for this transaction, or a new
                # operation observes the revocation and cannot begin.
                row = conn.execute(
                    """
                    SELECT a.role AS account_role, m.role AS project_role
                    FROM public.local_account a
                    JOIN public.project_membership m ON m.account_id = a.id
                    JOIN public.project_workspace w ON w.project_id = m.project_id
                    WHERE a.id = %s AND m.project_id = %s AND a.active = TRUE
                    FOR SHARE OF a, m
                    """,
                    (self.actor_account_id, self.project_id),
                ).fetchone()
                effective = (
                    row["project_role"]
                    if row and row["project_role"] in ("viewer", row["account_role"])
                    else None
                )
                if effective is None or (
                    self.required_role != "viewer" and effective != self.required_role
                ):
                    raise PermissionError("Project access changed before transaction")
            return conn
        except Exception:
            conn.close()
            raise

    @staticmethod
    def _apply_base_schema(conn) -> None:
        for statement in _SCHEMA:
            conn.execute(statement)
        _record_schema_revision(conn, "control", BASE_SCHEMA_VERSION)

    def migrate(self) -> None:
        """Atomic, serialized, idempotent control-plane schema migration."""
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            self._apply_base_schema(conn)

    def preview_run(
        self, *, approved_oracle_id: UUID | None,
        execution_backend: str = "memory", step_budget: int = 4,
    ) -> dict:
        """Read-only admission estimate, never a quota or target reservation.

        The actual admit transaction independently validates the approval and
        serializes the open-run quota. Native target capacity is checked later,
        at worker claim time, and cannot be promised by a composer preview.
        """
        if execution_backend not in ("memory", "native-postgresql"):
            raise ValueError("Unsupported execution backend")
        if not 4 <= step_budget <= 32:
            raise ValueError("Step budget must be between 4 and 32")
        with self.connect() as conn:
            quota = conn.execute(
                "SELECT max_open FROM project_quota WHERE project_id = 'local'"
            ).fetchone()
            max_open = quota["max_open"] if quota else 20
            open_runs = conn.execute(
                """
                SELECT count(*) AS n FROM experiment_run
                WHERE project_id = 'local'
                  AND status IN ('PENDING', 'RUNNING', 'CANCELLING')
                """
            ).fetchone()["n"]
            approval = (
                conn.execute(
                    """
                    SELECT state, rule_hash, target_revision, reviewed_by
                    FROM oracle_proposal WHERE id = %s
                    """,
                    (approved_oracle_id,),
                ).fetchone()
                if approved_oracle_id else None
            )
        approved = bool(
            approval and approval["state"] == "APPROVED"
            and approval["rule_hash"] == ORACLE_HASH
            and approval["target_revision"] == TARGET_REVISION
            and approval["reviewed_by"]
        )
        backend_configured = execution_backend == "memory"
        if execution_backend == "native-postgresql":
            target_location = (
                os.getenv("CLASHBOLT_TARGET_CLUSTER_URL")
                or os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
            )
            if target_location:
                try:
                    validate_target_cluster(target_location, self.dsn)
                    backend_configured = True
                except ValueError:
                    backend_configured = False
        blockers = []
        if not approved:
            blockers.append("No current approved oracle for this project")
        if open_runs >= max_open:
            blockers.append("Active experiment quota is full")
        if not backend_configured:
            blockers.append("Separate native target cluster is not configured")
        return {
            "project_id": self.project_id,
            "step_budget": step_budget,
            "open_runs": open_runs,
            "max_open": max_open,
            "oracle_approved": approved,
            "backend_configured": backend_configured,
            "preflight_ok": not blockers,
            "blockers": blockers,
            "reservation_made": False,
            "resource_bound": {
                "checkpoint_limit": step_budget,
                "disposable_databases": int(execution_backend == "native-postgresql"),
                "cpu_seconds_estimate": None,
            },
        }

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
            target_location = (
                os.getenv("CLASHBOLT_TARGET_CLUSTER_URL")
                or os.getenv("CLASHBOLT_TARGET_ADMIN_URL")
            )
            if not target_location:
                raise ValueError("Separate native target cluster is not configured")
            validate_target_cluster(target_location, self.dsn)
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
                return _public(existing, self.project_id)

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
            return _public(row, self.project_id)

    def get_draft(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT version, draft, updated_at
                FROM experiment_draft WHERE project_id = 'local'
                """
            ).fetchone()
            if not row:
                return None
            return {**row, "updated_at": row["updated_at"].isoformat()}

    @staticmethod
    def _validate_draft(draft: dict, expected_version: int) -> None:
        """Never persist capabilities, authorization or unsupported settings."""
        if not 0 <= expected_version <= 2**63 - 2:
            raise ValueError("Invalid draft version")
        if draft.get("variant") not in ("racy", "locked"):
            raise ValueError("Unsupported draft variant")
        if draft.get("schedule") not in SCHEDULES:
            raise ValueError("Unsupported draft schedule")
        if draft.get("execution_backend") not in ("memory", "native-postgresql"):
            raise ValueError("Unsupported draft target backend")
        if set(draft) != {"variant", "schedule", "execution_backend"}:
            raise ValueError("Draft has unsupported fields")

    def save_draft(self, draft: dict, expected_version: int) -> dict:
        """Version-CAS recovery; never infer approval from a saved draft."""
        self._validate_draft(draft, expected_version)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            if expected_version == 0:
                row = conn.execute(
                    """
                    INSERT INTO experiment_draft(project_id, version, draft)
                    VALUES ('local', 1, %s)
                    ON CONFLICT (project_id) DO NOTHING
                    RETURNING version, draft, updated_at
                    """,
                    (Jsonb(draft),),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    UPDATE experiment_draft
                    SET version = version + 1, draft = %s, updated_at = now()
                    WHERE project_id = 'local' AND version = %s
                    RETURNING version, draft, updated_at
                    """,
                    (Jsonb(draft), expected_version),
                ).fetchone()
            if not row:
                raise ValueError("Stale experiment draft version")
            return {**row, "updated_at": row["updated_at"].isoformat()}

    def heartbeat(self, owner: str) -> None:
        """A worker reports liveness explicitly, never inferred from API uptime."""
        if not 1 <= len(owner) <= 100:
            raise ValueError("Invalid worker identity")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_heartbeat(owner)
                VALUES (%s)
                ON CONFLICT (owner)
                DO UPDATE SET last_seen = now()
                """,
                (owner,),
            )

    def operations(self) -> dict:
        """Snapshot counts only: no worker secrets or target DSNs."""
        with self.connect() as conn:
            runs = conn.execute(
                """
                SELECT status, count(*) AS count FROM experiment_run
                WHERE project_id = 'local' GROUP BY status
                """
            ).fetchall()
            verifications = conn.execute(
                "SELECT status, count(*) AS count FROM verification_job GROUP BY status"
            ).fetchall()
            # Base-only ledger installations have no policy schema. Keep the
            # operational projection usable in both modes, never fabricate a
            # role-sensitive agent finding from a corrupt decision record.
            if conn.execute("SELECT to_regclass('agent_plan') AS name").fetchone()["name"]:
                quarantined = conn.execute(
                    """
                    SELECT count(*) AS count FROM agent_plan
                    WHERE state = 'UNKNOWN'
                      AND result->>'quarantined' = 'true'
                    """
                ).fetchone()["count"]
            else:
                quarantined = 0
            native_slots = conn.execute(
                """
                SELECT count(*) AS occupied FROM experiment_run
                WHERE project_id = 'local'
                  AND execution_backend = 'native-postgresql'
                  AND status IN ('RUNNING', 'CANCELLING')
                  AND lease_until > now()
                """
            ).fetchone()["occupied"]
            quota = conn.execute(
                "SELECT max_native_active FROM project_quota WHERE project_id = 'local'"
            ).fetchone()
            pending_age = conn.execute(
                """
                SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))
                    AS oldest_pending_seconds
                FROM experiment_run
                WHERE project_id = 'local' AND status = 'PENDING'
                """
            ).fetchone()["oldest_pending_seconds"]
            latency = conn.execute(
                """
                SELECT percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (
                        t.first_started - r.created_at
                    ))::double precision
                ) AS start_p95_seconds
                FROM experiment_run r
                JOIN LATERAL (
                    SELECT min(created_at) AS first_started
                    FROM run_transition
                    WHERE run_id = r.id AND status = 'RUNNING'
                ) t ON t.first_started IS NOT NULL
                WHERE r.project_id = 'local'
                  AND r.created_at >= now() - interval '7 days'
                """
            ).fetchone()["start_p95_seconds"]
            workers = conn.execute(
                """
                SELECT owner, last_seen, last_seen > now() - interval '15 seconds'
                    AS responsive FROM worker_heartbeat
                WHERE last_seen > now() - interval '1 day'
                ORDER BY last_seen DESC LIMIT 20
                """
            ).fetchall()
            return {
                "runs": {row["status"]: row["count"] for row in runs},
                "verifications": {
                    row["status"]: row["count"] for row in verifications
                },
                "workers": [
                    {
                        "owner": row["owner"],
                        "responsive": row["responsive"],
                        "last_seen": row["last_seen"].isoformat(),
                    }
                    for row in workers
                ],
                "responsive_workers": sum(row["responsive"] for row in workers),
                "quarantined_agent_plans": quarantined,
                "native_slots_occupied": native_slots,
                "native_slots_limit": quota["max_native_active"] if quota else 2,
                "oldest_pending_seconds": max(0.0, float(pending_age or 0)),
                "start_p95_seconds": max(0.0, float(latency or 0)),
            }

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
            return [_public(row, self.project_id) for row in rows]

    def get(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s AND project_id = 'local'",
                (run_id,),
            ).fetchone()
            return _public(row, self.project_id) if row else None

    def transition_page(
        self, run_id: UUID, after: int = 0, limit: int = 100
    ) -> dict:
        """Sequence-ordered pages; a missing SSE notification never loses history."""
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid event cursor or page limit")
        with self.connect() as conn:
            events = conn.execute(
                """
                SELECT version, status, note, created_at
                FROM run_transition WHERE run_id = %s AND version > %s
                ORDER BY version ASC LIMIT %s
                """,
                (run_id, after, limit + 1),
            ).fetchall()
        more = len(events) > limit
        visible = events[:limit]
        return {
            "events": [
                {**event, "created_at": event["created_at"].isoformat()}
                for event in visible
            ],
            "next_after": visible[-1]["version"] if visible else after,
            "has_more": more,
        }

    def transitions(self, run_id: UUID, after: int = 0) -> list[dict]:
        return self.transition_page(run_id, after)["events"]

    def _global_native_active(self, conn) -> int:
        """Count all live native run and verification claims in trusted schemas.

        Call only with GLOBAL_NATIVE_LOCK_ID held. The schema names come from
        the administrator-created registry, never an HTTP or agent-supplied ID.
        Expired claims do not reserve a slot; their effects remain UNKNOWN
        until the normal fenced reconciliation path.
        """
        workspaces = conn.execute(
            "SELECT schema_name FROM public.project_workspace ORDER BY project_id"
        ).fetchall()
        occupied = 0
        for workspace in workspaces:
            schema = workspace["schema_name"]
            if not _PROJECT_SCHEMA_NAME.fullmatch(schema):
                raise ValueError("Untrusted registered project schema")
            row = conn.execute(
                sql.SQL(
                    """
                    SELECT
                        (SELECT count(*) FROM {}.experiment_run
                         WHERE execution_backend = 'native-postgresql'
                           AND status IN ('RUNNING', 'CANCELLING')
                           AND lease_until > now())
                      + (SELECT count(*) FROM {}.verification_job
                         WHERE status = 'RUNNING' AND lease_until > now())
                        AS occupied
                    """
                ).format(sql.Identifier(schema), sql.Identifier(schema))
            ).fetchone()
            occupied += row["occupied"]
        return occupied

    def claim(self, owner: str, lease_seconds: int = 30) -> dict | None:
        if not owner or len(owner) > 100 or not 5 <= lease_seconds <= 300:
            raise ValueError("Invalid worker or lease duration")
        with self.connect() as conn:
            # The shared advisory lock comes *before* the local quota row.
            # Otherwise workers in different project schemas can each see
            # spare local capacity while jointly exceeding native host limits.
            global_budget_available = True
            if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts":
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (GLOBAL_NATIVE_LOCK_ID,)
                )
                global_budget_available = (
                    self._global_native_active(conn) < GLOBAL_NATIVE_SLOTS
                )
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            quota = conn.execute(
                """
                SELECT max_native_active FROM project_quota
                WHERE project_id = 'local' FOR UPDATE
                """
            ).fetchone()
            row = conn.execute(
                """
                SELECT * FROM experiment_run
                WHERE project_id = 'local'
                  AND (
                    status = 'PENDING'
                    OR (status = 'RUNNING' AND lease_until < now()
                        AND cancel_requested = FALSE)
                  )
                  AND (
                    execution_backend = 'memory'
                    OR (
                        (
                            SELECT count(*) FROM experiment_run AS active
                            WHERE active.project_id = 'local'
                              AND active.execution_backend = 'native-postgresql'
                              AND active.status IN ('RUNNING', 'CANCELLING')
                              AND active.lease_until > now()
                        ) < %s
                        AND %s
                    )
                  )
                ORDER BY created_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (quota["max_native_active"], global_budget_available),
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
            return _public(claimed, self.project_id)

    def track_target(
        self, run_id: UUID, generation: int, owner: str, instance_id: UUID
    ) -> bool:
        """Record the exact claim instance before touching the target cluster."""
        with self.connect() as conn:
            run = conn.execute(
                "SELECT status, lease_owner, lease_generation, lease_until "
                "FROM experiment_run WHERE id = %s FOR SHARE",
                (run_id,),
            ).fetchone()
            if (
                not run
                or run["status"] != "RUNNING"
                or run["lease_owner"] != owner
                or run["lease_generation"] != generation
                or run["lease_until"]
                    <= conn.execute("SELECT now() AS now").fetchone()["now"]
            ):
                return False
            conn.execute(
                """
                INSERT INTO target_instance(
                    id, run_id, lease_generation, owner, status
                ) VALUES (%s, %s, %s, %s, 'PROVISIONING')
                """,
                (instance_id, run_id, generation, owner),
            )
            return True

    def mark_target(self, instance_id: UUID, status: str) -> None:
        """Target records are facts; do not infer run success from cleanup."""
        if status not in ("ACTIVE", "CLEANED", "UNKNOWN"):
            raise ValueError("Invalid target lifecycle status")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE target_instance SET status = %s, updated_at = now()
                WHERE id = %s AND status != 'CLEANED'
                """,
                (status, instance_id),
            )

    def claim_abandoned_targets(self, limit: int = 8) -> list[UUID]:
        """Lease-fenced cleanup work; target IDs are derived, never supplied by API."""
        if not 1 <= limit <= 50:
            raise ValueError("Invalid cleanup batch size")
        with self.connect() as conn:
            targets = conn.execute(
                """
                SELECT t.id FROM target_instance t
                JOIN experiment_run r ON r.id = t.run_id
                WHERE t.status IN ('PROVISIONING', 'ACTIVE', 'UNKNOWN', 'CLEANING')
                AND t.updated_at < now() - interval '5 seconds'
                AND (
                    r.lease_generation != t.lease_generation
                    OR r.status != 'RUNNING'
                    OR r.lease_until < now()
                )
                ORDER BY t.created_at, t.id
                LIMIT %s FOR UPDATE OF t SKIP LOCKED
                """,
                (limit,),
            ).fetchall()
            for row in targets:
                conn.execute(
                    """
                    UPDATE target_instance SET status = 'CLEANING',
                        updated_at = now() WHERE id = %s
                    """,
                    (row["id"],),
                )
            return [item["id"] for item in targets]

    def target_instances(self, run_id: UUID) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, lease_generation, status
                FROM target_instance WHERE run_id = %s
                ORDER BY lease_generation
                """,
                (run_id,),
            ).fetchall()
            return [
                {**row, "id": str(row["id"])}
                for row in rows
            ]

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

    def record_reduction(
        self, run_id: UUID, version: int, report: dict
    ) -> dict | None:
        """Version-fenced report: never attach it to a revoked finding."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, verdict, version, witness FROM experiment_run "
                "WHERE id = %s AND project_id = 'local' FOR UPDATE",
                (run_id,),
            ).fetchone()
            if (
                not row
                or row["status"] != "COMPLETE"
                or row["verdict"] != "FAIL"
                or row["version"] != version
                or report.get("original_evidence_sha256")
                != row["witness"]["evidence_sha256"]
            ):
                return None
            conn.execute(
                """
                INSERT INTO run_reduction(run_id, run_version, report)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id, run_version) DO NOTHING
                """,
                (run_id, version, Jsonb(report)),
            )
            return report

    def get_reduction(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.report FROM run_reduction r
                JOIN experiment_run e ON e.id = r.run_id
                    AND e.version = r.run_version
                WHERE r.run_id = %s AND e.project_id = 'local'
                    AND e.status = 'COMPLETE' AND e.verdict = 'FAIL'
                """,
                (run_id,),
            ).fetchone()
            return row["report"] if row else None

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
            return _public(row, self.project_id)

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
