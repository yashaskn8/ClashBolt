"""PostgreSQL control-plane ledger for local synthetic experiments.

All state transitions, queue claims, and events commit in the same transaction.
This module deliberately does not connect to or execute an external target.
"""

import hashlib
import json
import math
import os
import re
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from clashbolt.accounting import (
    DEFAULT_MAX_RESERVED_CPU_MS,
    DEFAULT_RESERVED_CPU_MS,
    MAX_RESERVED_CPU_MS,
    MIN_RESERVED_CPU_MS,
    build_reservation,
    reconcile_run_accounting,
    released_run_accounting,
    uncertain_run_accounting,
)
from clashbolt.experiment import ORACLE_HASH, SCHEDULES, TARGET_REVISION
from clashbolt.target_db import validate_target_cluster
from clashbolt.witness import validate_contract

SCHEMA_LOCK_ID = 538192047
GLOBAL_NATIVE_LOCK_ID = 538192048
GLOBAL_NATIVE_SLOTS = 2
MAX_CLAIM_GENERATIONS = 3
BASE_SCHEMA_VERSION = 11
MAX_EVENT_VERSION = 2**63 - 1
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
    "ALTER TABLE project_quota ADD COLUMN IF NOT EXISTS "
    "max_reserved_cpu_ms INTEGER NOT NULL DEFAULT 180000 "
    "CHECK (max_reserved_cpu_ms BETWEEN 500 AND 3600000)",
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
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "reserved_cpu_ms INTEGER NOT NULL DEFAULT 3000 "
    "CHECK (reserved_cpu_ms BETWEEN 500 AND 30000)",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "reserved_target_dbs INTEGER NOT NULL DEFAULT 0 "
    "CHECK (reserved_target_dbs BETWEEN 0 AND 1)",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "accounting_state TEXT NOT NULL DEFAULT 'RESERVED' "
    "CHECK (accounting_state IN ('RESERVED', 'RECONCILED', 'PARTIAL', 'UNCERTAIN', 'RELEASED'))",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "measured_worker_cpu_ms DOUBLE PRECISION "
    "CHECK (measured_worker_cpu_ms IS NULL OR measured_worker_cpu_ms >= 0)",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "measured_app_wall_ms DOUBLE PRECISION "
    "CHECK (measured_app_wall_ms IS NULL OR measured_app_wall_ms >= 0)",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS "
    "measured_db_active_ms DOUBLE PRECISION "
    "CHECK (measured_db_active_ms IS NULL OR measured_db_active_ms >= 0)",
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS resource_accounting JSONB",
    """
    CREATE INDEX IF NOT EXISTS experiment_claim_idx
    ON experiment_run (status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS experiment_run_project_created_idx
    ON experiment_run (project_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS experiment_run_preview_complete_idx
    ON experiment_run (project_id, execution_backend, created_at DESC)
    WHERE status = 'COMPLETE' AND measured_worker_cpu_ms IS NOT NULL
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
    "ALTER TABLE target_instance ADD COLUMN IF NOT EXISTS "
    "provision_ms DOUBLE PRECISION "
    "CHECK (provision_ms IS NULL OR provision_ms >= 0)",
    "ALTER TABLE target_instance ADD COLUMN IF NOT EXISTS "
    "teardown_ms DOUBLE PRECISION "
    "CHECK (teardown_ms IS NULL OR teardown_ms >= 0)",
    """
    CREATE INDEX IF NOT EXISTS target_instance_cleanup_idx
    ON target_instance (status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS target_instance_created_idx
    ON target_instance (created_at)
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
    CREATE INDEX IF NOT EXISTS run_transition_cancel_idx
    ON run_transition (created_at)
    WHERE note = 'operator requested cancellation'
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
    "ALTER TABLE verification_target_instance ADD COLUMN IF NOT EXISTS "
    "provision_ms DOUBLE PRECISION "
    "CHECK (provision_ms IS NULL OR provision_ms >= 0)",
    "ALTER TABLE verification_target_instance ADD COLUMN IF NOT EXISTS "
    "teardown_ms DOUBLE PRECISION "
    "CHECK (teardown_ms IS NULL OR teardown_ms >= 0)",
    """
    CREATE TABLE IF NOT EXISTS witness_export (
        id UUID PRIMARY KEY,
        run_id UUID NOT NULL REFERENCES experiment_run(id),
        run_version BIGINT NOT NULL,
        evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
        artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
        artifact_bytes INTEGER NOT NULL CHECK(artifact_bytes BETWEEN 1 AND 1000000),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(run_id, run_version)
    )
    """,
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_witness_export()
    RETURNS TRIGGER LANGUAGE plpgsql AS $export_guard$
    BEGIN
        RAISE EXCEPTION 'Witness export audit is append-only';
    END
    $export_guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_witness_export_immutable ON witness_export",
    """
    CREATE TRIGGER clashbolt_witness_export_immutable
    BEFORE UPDATE OR DELETE ON witness_export FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_witness_export()
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
    variant: str,
    schedule: str,
    budget: int,
    approval_id: UUID,
    execution_backend: str,
    reserved_cpu_ms: int = DEFAULT_RESERVED_CPU_MS,
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
    if reserved_cpu_ms != DEFAULT_RESERVED_CPU_MS:
        request["reserved_cpu_ms"] = reserved_cpu_ms
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
        "reserved_cpu_ms": row["reserved_cpu_ms"],
        "accounting_state": row["accounting_state"],
        "resource_accounting": row["resource_accounting"],
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


def validate_target_latencies(
    provision_ms: float | None, teardown_ms: float | None
) -> tuple[float | None, float | None]:
    """Validate and round optional non-negative finite target lifecycle latencies."""
    validated: list[float | None] = []
    for latency in (provision_ms, teardown_ms):
        if latency is None:
            validated.append(None)
        elif (
            type(latency) not in (int, float)
            or not math.isfinite(float(latency))
            or float(latency) < 0
        ):
            raise ValueError("Target latency must be a non-negative finite number")
        else:
            validated.append(round(float(latency), 3))
    return validated[0], validated[1]


class Ledger:
    def __init__(
        self, dsn: str, *, schema: str = "public", project_id: str = "local",
        actor_account_id: UUID | None = None, required_role: str = "viewer",
        actor_session_epoch: int | None = None,
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
        self.actor_session_epoch = actor_session_epoch
        self.required_role = required_role

    def connect(self) -> psycopg.Connection:
        if not _PROJECT_SCHEMA_NAME.fullmatch(self.schema):
            raise ValueError("Unrecognized project database schema")
        # Never fall back to public from an isolated project: a missing project
        # table must error, not silently expose another workspace. Also fail
        # closed on runaway analytical scans or contended lock chains via
        # connection startup parameters (saving 3 round-trips per connection).
        conn = psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            connect_timeout=5,
            options=(
                f"-c search_path={self.schema} "
                "-c statement_timeout=30000 "
                "-c lock_timeout=10000"
            ),
        )
        try:
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
                      AND a.session_epoch = COALESCE(%s, a.session_epoch)
                    FOR SHARE OF a, m
                    """,
                    (self.actor_account_id, self.project_id, self.actor_session_epoch),
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
        reserved_cpu_ms: int = DEFAULT_RESERVED_CPU_MS,
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
        if type(reserved_cpu_ms) is not int or not (
            MIN_RESERVED_CPU_MS <= reserved_cpu_ms <= MAX_RESERVED_CPU_MS
        ):
            raise ValueError("Reserved CPU budget must be 500-30000 ms")
        with self.connect() as conn:
            quota = conn.execute(
                "SELECT max_open, max_reserved_cpu_ms FROM project_quota WHERE project_id = 'local'"
            ).fetchone()
            max_open = quota["max_open"] if quota else 20
            max_reserved_cpu_ms = (
                quota["max_reserved_cpu_ms"] if quota else DEFAULT_MAX_RESERVED_CPU_MS
            )
            open_summary = conn.execute(
                """
                SELECT count(*) AS n,
                       COALESCE(sum(reserved_cpu_ms), 0) AS open_reserved_cpu_ms
                FROM experiment_run
                WHERE project_id = 'local'
                  AND status IN ('PENDING', 'RUNNING', 'CANCELLING')
                """
            ).fetchone()
            open_runs = open_summary["n"]
            open_reserved_cpu_ms = open_summary["open_reserved_cpu_ms"]
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
            hist = conn.execute(
                """
                SELECT
                    count(*) AS sample_count,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY measured_worker_cpu_ms)
                        AS median_worker_cpu_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY measured_worker_cpu_ms)
                        AS p95_worker_cpu_ms,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY measured_app_wall_ms)
                        AS median_app_wall_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY measured_app_wall_ms)
                        AS p95_app_wall_ms,
                    count(measured_db_active_ms) AS db_sample_count,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY measured_db_active_ms)
                        FILTER (WHERE measured_db_active_ms IS NOT NULL)
                        AS median_db_active_ms,
                    count(*) FILTER (
                        WHERE (resource_accounting->'reconciliation'->>'overspent') = 'true'
                    ) AS overspent_samples
                FROM experiment_run
                WHERE project_id = 'local'
                  AND execution_backend = %s
                  AND status = 'COMPLETE'
                  AND accounting_state IN ('RECONCILED', 'PARTIAL')
                  AND measured_worker_cpu_ms IS NOT NULL
                  AND created_at >= now() - interval '7 days'
                """,
                (execution_backend,),
            ).fetchone()
            reset_hist = (
                conn.execute(
                    """
                    SELECT
                        count(*) AS sample_count,
                        percentile_cont(0.5) WITHIN GROUP (ORDER BY provision_ms + teardown_ms)
                            AS median_target_reset_ms
                    FROM target_instance t
                    JOIN experiment_run r ON r.id = t.run_id
                    WHERE r.project_id = 'local'
                      AND r.execution_backend = 'native-postgresql'
                      AND t.status = 'CLEANED'
                      AND t.provision_ms IS NOT NULL
                      AND t.teardown_ms IS NOT NULL
                      AND t.created_at >= now() - interval '7 days'
                    """
                ).fetchone()
                if execution_backend == "native-postgresql"
                else None
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
        cpu_budget_available = open_reserved_cpu_ms + reserved_cpu_ms <= max_reserved_cpu_ms
        blockers = []
        if not approved:
            blockers.append("No current approved oracle for this project")
        if open_runs >= max_open:
            blockers.append("Active experiment quota is full")
        if not cpu_budget_available:
            blockers.append("Active CPU reservation budget is full")
        if not backend_configured:
            blockers.append("Separate native target cluster is not configured")

        sample_count = int(hist["sample_count"] or 0)
        if sample_count == 0:
            uncertainty = "UNAVAILABLE"
            uncertainty_reason = (
                "No recent reconciled runs for this backend in this project; "
                "resource cost is not estimated."
            )
        elif sample_count < 5:
            uncertainty = "LOW_SAMPLE_SIZE"
            uncertainty_reason = (
                f"Based on {sample_count} local synthetic run(s) over 7 days; "
                "sample size is below 5 and external provider billing is excluded."
            )
        else:
            uncertainty = "LOCAL_SYNTHETIC_ONLY"
            uncertainty_reason = (
                f"Based on {sample_count} local synthetic runs over 7 days; "
                "workload variance and external provider billing are excluded."
            )

        def _round_opt(value: object, digits: int = 3) -> float | None:
            return round(max(0.0, float(value)), digits) if value is not None else None

        median_cpu_ms = _round_opt(hist["median_worker_cpu_ms"])
        cpu_seconds_estimate = (
            round(median_cpu_ms / 1000.0, 4)
            if sample_count > 0 and median_cpu_ms is not None
            else None
        )
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
                "cpu_seconds_estimate": cpu_seconds_estimate,
            },
            "budget": {
                "reserved_cpu_ms_request": reserved_cpu_ms,
                "open_reserved_cpu_ms": open_reserved_cpu_ms,
                "max_reserved_cpu_ms": max_reserved_cpu_ms,
                "cpu_budget_available": cpu_budget_available,
                "measured_cost_status": (
                    "HISTORICAL_ESTIMATE_ONLY"
                    if sample_count > 0
                    else "UNAVAILABLE_BEFORE_EXECUTION"
                ),
                "provider_cost_estimate_usd": None,
            },
            "historical_estimate": {
                "basis": "PROJECT_MEASURED_7D",
                "execution_backend": execution_backend,
                "sample_count": sample_count,
                "uncertainty": uncertainty,
                "uncertainty_reason": uncertainty_reason,
                "median_worker_cpu_ms": median_cpu_ms,
                "p95_worker_cpu_ms": _round_opt(hist["p95_worker_cpu_ms"]),
                "median_app_wall_ms": _round_opt(hist["median_app_wall_ms"]),
                "p95_app_wall_ms": _round_opt(hist["p95_app_wall_ms"]),
                "db_sample_count": int(hist["db_sample_count"] or 0),
                "median_db_active_ms": (
                    _round_opt(hist["median_db_active_ms"])
                    if execution_backend == "native-postgresql"
                    and (hist["db_sample_count"] or 0) > 0
                    else None
                ),
                "median_target_reset_ms": (
                    _round_opt(reset_hist["median_target_reset_ms"])
                    if reset_hist and (reset_hist["sample_count"] or 0) > 0
                    else None
                ),
                "overspent_samples": int(hist["overspent_samples"] or 0),
                "provider_cost_usd": None,
                "provider_cost_status": "UNAVAILABLE",
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
        reserved_cpu_ms: int = DEFAULT_RESERVED_CPU_MS,
    ) -> dict:
        """A retry with the same key+request returns the same run; conflicts reject."""
        if variant not in ("racy", "locked") or schedule not in SCHEDULES:
            raise ValueError("Unknown built-in fixture variant or schedule")
        if not 4 <= step_budget <= 32:
            raise ValueError("Step budget must be between 4 and 32")
        if type(reserved_cpu_ms) is not int or not (
            MIN_RESERVED_CPU_MS <= reserved_cpu_ms <= MAX_RESERVED_CPU_MS
        ):
            raise ValueError("Reserved CPU budget must be 500-30000 ms")
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
            variant, schedule, step_budget, approved_oracle_id,
            execution_backend, reserved_cpu_ms,
        )
        with self.connect() as conn:
            # The quota row serializes admission for this local workspace.
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            quota = conn.execute(
                """
                SELECT max_open, max_reserved_cpu_ms
                FROM project_quota WHERE project_id = 'local' FOR UPDATE
                """
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
            open_summary = conn.execute(
                """
                SELECT count(*) AS open_count,
                       COALESCE(sum(reserved_cpu_ms), 0) AS open_reserved_cpu_ms
                FROM experiment_run
                WHERE project_id = 'local'
                  AND status IN ('PENDING', 'RUNNING', 'CANCELLING')
                """
            ).fetchone()
            if open_summary["open_count"] >= quota["max_open"]:
                raise ValueError("Active experiment quota exhausted")
            if (
                open_summary["open_reserved_cpu_ms"] + reserved_cpu_ms
                > quota["max_reserved_cpu_ms"]
            ):
                raise ValueError("Active CPU reservation quota exhausted")
            accounting = build_reservation(
                execution_backend=execution_backend,
                step_budget=step_budget,
                reserved_cpu_ms=reserved_cpu_ms,
            )
            row = conn.execute(
                """
                INSERT INTO experiment_run (
                    id, project_id, client_key, request_hash,
                    variant, schedule, oracle_hash, step_budget, approved_oracle_id,
                    execution_backend, reserved_cpu_ms, reserved_target_dbs,
                    accounting_state, resource_accounting
                )
                VALUES (%s, 'local', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'RESERVED', %s)
                RETURNING *
                """,
                (
                    uuid4(), client_key, request_hash, variant, schedule,
                    ORACLE_HASH, step_budget, approved_oracle_id, execution_backend,
                    reserved_cpu_ms, int(execution_backend == "native-postgresql"),
                    Jsonb(accounting),
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
            exp_summary = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'PENDING') AS pending_count,
                    count(*) FILTER (WHERE status = 'RUNNING') AS running_count,
                    count(*) FILTER (WHERE status = 'CANCELLING') AS cancelling_count,
                    count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled_count,
                    count(*) FILTER (WHERE status = 'COMPLETE') AS complete_count,
                    count(*) FILTER (WHERE status = 'UNKNOWN') AS unknown_count,
                    count(*) FILTER (
                        WHERE execution_backend = 'native-postgresql'
                          AND status IN ('RUNNING', 'CANCELLING')
                          AND lease_until > now()
                    ) AS native_slots_occupied,
                    count(*) FILTER (
                        WHERE status IN ('PENDING', 'RUNNING', 'CANCELLING')
                    ) AS open_runs,
                    COALESCE(sum(reserved_cpu_ms) FILTER (
                        WHERE status IN ('PENDING', 'RUNNING', 'CANCELLING')
                    ), 0) AS open_reserved_cpu_ms,
                    COALESCE(sum(reserved_target_dbs) FILTER (
                        WHERE status IN ('PENDING', 'RUNNING', 'CANCELLING')
                    ), 0) AS open_reserved_target_dbs,
                    EXTRACT(EPOCH FROM (
                        now() - min(created_at) FILTER (WHERE status = 'PENDING')
                    ))::double precision AS oldest_pending_seconds,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS total_runs,
                    COALESCE(sum(reserved_cpu_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ), 0) AS reserved_cpu_ms_total,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state = 'RECONCILED'
                    ) AS reconciled_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state = 'PARTIAL'
                    ) AS partial_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state = 'UNCERTAIN'
                    ) AS uncertain_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state = 'RELEASED'
                    ) AS released_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state = 'RESERVED'
                    ) AS reserved_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND (resource_accounting->'reconciliation'->>'overspent') = 'true'
                    ) AS overspent_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND (resource_accounting->'reconciliation'->>'overspent')
                              IN ('true', 'false')
                          AND accounting_state != 'RELEASED'
                    ) AS overspend_evaluated_count,
                    count(*) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                          AND accounting_state IN ('UNCERTAIN', 'PARTIAL')
                          AND (resource_accounting->'reconciliation'->>'overspent') IS NULL
                    ) AS overspend_uncertain_count,
                    sum(measured_worker_cpu_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_worker_cpu_ms_total,
                    count(measured_worker_cpu_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_worker_cpu_samples,
                    sum(measured_app_wall_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_app_wall_ms_total,
                    count(measured_app_wall_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_app_wall_samples,
                    sum(measured_db_active_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_db_active_ms_total,
                    count(measured_db_active_ms) FILTER (
                        WHERE created_at >= now() - interval '7 days'
                    ) AS measured_db_active_samples
                FROM experiment_run
                WHERE project_id = 'local'
                """
            ).fetchone()
            verifications = conn.execute(
                "SELECT status, count(*) AS count FROM verification_job GROUP BY status"
            ).fetchall()
            # Base-only ledger installations have no policy schema. Check both
            # optional tables in one catalog round-trip.
            optional_tables = conn.execute(
                """
                SELECT to_regclass('agent_plan') AS agent_plan_tbl,
                       to_regclass('model_budget') AS model_budget_tbl
                """
            ).fetchone()
            if optional_tables["agent_plan_tbl"]:
                quarantined = conn.execute(
                    """
                    SELECT count(*) AS count FROM agent_plan
                    WHERE state = 'UNKNOWN'
                      AND result->>'quarantined' = 'true'
                    """
                ).fetchone()["count"]
            else:
                quarantined = 0
            quota = conn.execute(
                """
                SELECT max_open, max_native_active, max_reserved_cpu_ms
                FROM project_quota WHERE project_id = 'local'
                """
            ).fetchone()
            cancel_latency = conn.execute(
                """
                SELECT
                    count(*) AS sample_count,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (
                            term.completed_at - req.created_at
                        ))::double precision
                    ) AS p95_seconds,
                    max(
                        EXTRACT(EPOCH FROM (term.completed_at - req.created_at))::double precision
                    ) AS max_seconds
                FROM run_transition req
                JOIN experiment_run r ON r.id = req.run_id
                JOIN LATERAL (
                    SELECT min(t.created_at) AS completed_at
                    FROM run_transition t
                    WHERE t.run_id = req.run_id
                      AND t.version >= req.version
                      AND t.status IN ('CANCELLED', 'UNKNOWN')
                ) term ON term.completed_at IS NOT NULL
                WHERE r.project_id = 'local'
                  AND req.note = 'operator requested cancellation'
                  AND req.created_at >= now() - interval '7 days'
                """
            ).fetchone()
            if exp_summary["cancelling_count"] > 0:
                cancel_inflight = conn.execute(
                    """
                    SELECT
                        count(*) AS in_flight,
                        EXTRACT(EPOCH FROM (
                            now() - min(COALESCE(req.created_at, r.updated_at))
                        ))::double precision AS oldest_in_flight_seconds
                    FROM experiment_run r
                    LEFT JOIN LATERAL (
                        SELECT created_at FROM run_transition
                        WHERE run_id = r.id AND note = 'operator requested cancellation'
                        ORDER BY version DESC LIMIT 1
                    ) req ON TRUE
                    WHERE r.project_id = 'local' AND r.status = 'CANCELLING'
                    """
                ).fetchone()
            else:
                cancel_inflight = {"in_flight": 0, "oldest_in_flight_seconds": None}
            model_budget_summary = None
            if optional_tables["model_budget_tbl"]:
                mb_row = conn.execute(
                    """
                    SELECT call_limit, token_limit, calls_reserved, tokens_reserved,
                           tokens_observed
                    FROM model_budget
                    WHERE project_id = 'local' AND window_date = CURRENT_DATE
                    """
                ).fetchone()
                mr_row = conn.execute(
                    """
                    SELECT
                        count(*) FILTER (WHERE state = 'RESERVED') AS inflight,
                        count(*) FILTER (WHERE state = 'UNKNOWN') AS unknown,
                        count(*) FILTER (WHERE state = 'COMPLETE') AS complete,
                        COALESCE(sum(reserved_tokens) FILTER (
                            WHERE state = 'UNKNOWN' AND observed_tokens IS NULL
                        ), 0) AS tokens_uncertain_reserved
                    FROM model_reservation
                    WHERE project_id = 'local' AND window_date = CURRENT_DATE
                    """
                ).fetchone()
                calls_reserved = mb_row["calls_reserved"] if mb_row else 0
                tokens_observed = mb_row["tokens_observed"] if mb_row else 0
                model_budget_summary = {
                    "calls_reserved": calls_reserved,
                    "tokens_reserved": mb_row["tokens_reserved"] if mb_row else 0,
                    "tokens_observed": (
                        tokens_observed
                        if (calls_reserved == 0 or mr_row["complete"] > 0 or tokens_observed > 0)
                        else None
                    ),
                    "tokens_uncertain_reserved": mr_row["tokens_uncertain_reserved"],
                    "call_limit": mb_row["call_limit"] if mb_row else 20,
                    "token_limit": mb_row["token_limit"] if mb_row else 5120,
                    "calls_inflight": mr_row["inflight"],
                    "calls_unknown": mr_row["unknown"],
                    "provider_cost_status": "UNAVAILABLE",
                    "provider_cost_usd": None,
                }
            latency = conn.execute(
                """
                SELECT percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (
                        first_started - created_at
                    ))::double precision
                ) AS start_p95_seconds
                FROM (
                    SELECT r.created_at, min(t.created_at) AS first_started
                    FROM experiment_run r
                    JOIN run_transition t ON t.run_id = r.id AND t.status = 'RUNNING'
                    WHERE r.project_id = 'local'
                      AND r.created_at >= now() - interval '7 days'
                    GROUP BY r.id
                ) first_claims
                """
            ).fetchone()["start_p95_seconds"]
            target_reset = conn.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE status = 'CLEANED'
                          AND provision_ms IS NOT NULL
                          AND teardown_ms IS NOT NULL
                    ) AS sample_count,
                    count(*) FILTER (WHERE status = 'UNKNOWN') AS unknown_count,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY provision_ms)
                        FILTER (
                            WHERE status = 'CLEANED'
                              AND provision_ms IS NOT NULL
                              AND teardown_ms IS NOT NULL
                        ) AS provision_p95_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY teardown_ms)
                        FILTER (
                            WHERE status = 'CLEANED'
                              AND provision_ms IS NOT NULL
                              AND teardown_ms IS NOT NULL
                        ) AS teardown_p95_ms,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY provision_ms + teardown_ms)
                        FILTER (
                            WHERE status = 'CLEANED'
                              AND provision_ms IS NOT NULL
                              AND teardown_ms IS NOT NULL
                        ) AS reset_p95_ms
                FROM target_instance
                WHERE created_at >= now() - interval '7 days'
                """
            ).fetchone()
            workers = conn.execute(
                """
                SELECT owner, last_seen, last_seen > now() - interval '15 seconds'
                    AS responsive FROM worker_heartbeat
                WHERE last_seen > now() - interval '1 day'
                ORDER BY last_seen DESC LIMIT 20
                """
            ).fetchall()
            runs = {
                status: exp_summary[col]
                for status, col in (
                    ("PENDING", "pending_count"),
                    ("RUNNING", "running_count"),
                    ("CANCELLING", "cancelling_count"),
                    ("CANCELLED", "cancelled_count"),
                    ("COMPLETE", "complete_count"),
                    ("UNKNOWN", "unknown_count"),
                )
                if exp_summary[col] > 0
            }
            return {
                "runs": runs,
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
                "native_slots_occupied": exp_summary["native_slots_occupied"],
                "native_slots_limit": quota["max_native_active"] if quota else 2,
                "oldest_pending_seconds": max(
                    0.0, float(exp_summary["oldest_pending_seconds"] or 0)
                ),
                "start_p95_seconds": max(0.0, float(latency or 0)),
                "cancellation": {
                    "sample_count": cancel_latency["sample_count"],
                    "p95_seconds": (
                        round(max(0.0, float(cancel_latency["p95_seconds"])), 3)
                        if cancel_latency["p95_seconds"] is not None else None
                    ),
                    "max_seconds": (
                        round(max(0.0, float(cancel_latency["max_seconds"])), 3)
                        if cancel_latency["max_seconds"] is not None else None
                    ),
                    "in_flight": cancel_inflight["in_flight"],
                    "oldest_in_flight_seconds": (
                        round(max(0.0, float(cancel_inflight["oldest_in_flight_seconds"])), 3)
                        if cancel_inflight["in_flight"] > 0
                        and cancel_inflight["oldest_in_flight_seconds"] is not None
                        else None
                    ),
                },
                "resources": {
                    "project_id": self.project_id,
                    "open_runs": exp_summary["open_runs"],
                    "max_open": quota["max_open"] if quota else 20,
                    "open_reserved_cpu_ms": exp_summary["open_reserved_cpu_ms"],
                    "max_reserved_cpu_ms": (
                        quota["max_reserved_cpu_ms"] if quota else DEFAULT_MAX_RESERVED_CPU_MS
                    ),
                    "open_reserved_target_dbs": exp_summary["open_reserved_target_dbs"],
                    "provider_cost_status": "UNAVAILABLE",
                    "provider_cost_usd": None,
                    "runs_7d": {
                        "total_runs": exp_summary["total_runs"],
                        "reserved_cpu_ms_total": exp_summary["reserved_cpu_ms_total"],
                        "reconciled_count": exp_summary["reconciled_count"],
                        "partial_count": exp_summary["partial_count"],
                        "uncertain_count": exp_summary["uncertain_count"],
                        "released_count": exp_summary["released_count"],
                        "reserved_count": exp_summary["reserved_count"],
                        "overspent_count": exp_summary["overspent_count"],
                        "overspend_evaluated_count": exp_summary["overspend_evaluated_count"],
                        "overspend_uncertain_count": exp_summary["overspend_uncertain_count"],
                        "overspend_rate": (
                            round(
                                exp_summary["overspent_count"]
                                / exp_summary["overspend_evaluated_count"],
                                4,
                            )
                            if exp_summary["overspend_evaluated_count"] > 0
                            else None
                        ),
                        "measured_worker_cpu_ms_total": (
                            round(float(exp_summary["measured_worker_cpu_ms_total"]), 3)
                            if exp_summary["measured_worker_cpu_samples"] > 0
                            else None
                        ),
                        "measured_worker_cpu_samples": exp_summary["measured_worker_cpu_samples"],
                        "measured_app_wall_ms_total": (
                            round(float(exp_summary["measured_app_wall_ms_total"]), 3)
                            if exp_summary["measured_app_wall_samples"] > 0
                            else None
                        ),
                        "measured_app_wall_samples": exp_summary["measured_app_wall_samples"],
                        "measured_db_active_ms_total": (
                            round(float(exp_summary["measured_db_active_ms_total"]), 3)
                            if exp_summary["measured_db_active_samples"] > 0
                            else None
                        ),
                        "measured_db_active_samples": exp_summary["measured_db_active_samples"],
                    },
                    "model_budget": model_budget_summary,
                },
                "target_reset": {
                    "sample_count": target_reset["sample_count"],
                    "unknown_count": target_reset["unknown_count"],
                    "provision_p95_ms": (
                        round(max(0.0, float(target_reset["provision_p95_ms"])), 3)
                        if target_reset["provision_p95_ms"] is not None else None
                    ),
                    "teardown_p95_ms": (
                        round(max(0.0, float(target_reset["teardown_p95_ms"])), 3)
                        if target_reset["teardown_p95_ms"] is not None else None
                    ),
                    "reset_p95_ms": (
                        round(max(0.0, float(target_reset["reset_p95_ms"])), 3)
                        if target_reset["reset_p95_ms"] is not None else None
                    ),
                },
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

    @staticmethod
    def _public_export(row: dict, current_version: int, current_status: str) -> dict:
        return {
            "export_id": str(row["id"]),
            "run_id": str(row["run_id"]),
            "run_version": row["run_version"],
            "evidence_sha256": row["evidence_sha256"],
            "artifact_sha256": row["artifact_sha256"],
            "artifact_bytes": row["artifact_bytes"],
            "created_at": row["created_at"].isoformat(),
            "current": (
                current_status == "COMPLETE"
                and row["run_version"] == current_version
            ),
            "scope": "SYNTHETIC_ONLY",
        }

    def record_bundle_export(self, run_id: UUID) -> dict | None:
        """Freeze a verifiable ZIP digest without persisting or executing target code.

        A read-only bundle download remains separate: operators must first
        request this audited export. The run lock fences concurrent revocation
        so the recorded digest always belongs to the exact certified version.
        """
        from clashbolt.witness_bundle import create_bundle

        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT * FROM experiment_run
                WHERE id = %s AND project_id = 'local' FOR SHARE
                """,
                (run_id,),
            ).fetchone()
            if not run:
                return None
            if (
                run["status"] != "COMPLETE"
                or run["verdict"] not in ("PASS", "FAIL")
                or not isinstance(run["witness"], dict)
            ):
                raise ValueError("A current certified synthetic witness is required")
            bundle = create_bundle(run["witness"])
            artifact_sha = hashlib.sha256(bundle).hexdigest()
            evidence_sha = run["witness"]["evidence_sha256"]
            conn.execute(
                """
                INSERT INTO witness_export(
                    id, run_id, run_version, evidence_sha256,
                    artifact_sha256, artifact_bytes
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, run_version) DO NOTHING
                """,
                (
                    uuid4(), run_id, run["version"], evidence_sha,
                    artifact_sha, len(bundle),
                ),
            )
            entry = conn.execute(
                """
                SELECT * FROM witness_export
                WHERE run_id = %s AND run_version = %s
                """,
                (run_id, run["version"]),
            ).fetchone()
            if (
                not entry
                or entry["artifact_sha256"] != artifact_sha
                or entry["evidence_sha256"] != evidence_sha
                or entry["artifact_bytes"] != len(bundle)
            ):
                raise ValueError("Immutable evidence export does not match current ZIP")
            return self._public_export(entry, run["version"], run["status"])

    def bundle_exports(self, run_id: UUID) -> dict | None:
        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT version, status FROM experiment_run
                WHERE id = %s AND project_id = 'local'
                """,
                (run_id,),
            ).fetchone()
            if not run:
                return None
            rows = conn.execute(
                """
                SELECT * FROM witness_export WHERE run_id = %s
                ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            return {
                "run_id": str(run_id),
                "project_id": self.project_id,
                "exports": [
                    self._public_export(row, run["version"], run["status"])
                    for row in rows
                ],
            }

    @staticmethod
    def _current_replay(conn, run: dict) -> dict | None:
        """Only a worker-certified, current synthetic replay is reviewable."""
        if (
            run["status"] != "COMPLETE"
            or run["verdict"] != "FAIL"
            or not isinstance(run["witness"], dict)
            or validate_contract(run["witness"]) is not None
        ):
            return None
        replay = conn.execute(
            """
            SELECT id, status, result FROM verification_job
            WHERE run_id = %s AND run_version = %s AND kind = 'REPLAY'
            """,
            (run["id"], run["version"]),
        ).fetchone()
        if not replay or replay["status"] != "COMPLETE":
            return None
        report = replay["result"] or {}
        digest = run["witness"]["evidence_sha256"]
        if (
            report.get("status") != "REPRODUCED"
            or report.get("scope") != "SYNTHETIC_ONLY"
            or report.get("original_sha256") != digest
            or report.get("replay_sha256") != digest
            or report.get("verdict") != "FAIL"
        ):
            return None
        return replay

    def witness_reviews(self, run_id: UUID) -> dict | None:
        """Display evidence-bound history; expired reviews never stay current."""
        with self.connect() as conn:
            run = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s AND project_id = 'local'",
                (run_id,),
            ).fetchone()
            if not run:
                return None
            replay = self._current_replay(conn, run)
            evidence_hash = (
                run["witness"].get("evidence_sha256")
                if isinstance(run["witness"], dict) else None
            )
            rows = conn.execute(
                """
                SELECT id, run_version, evidence_sha256, reviewer_account_id,
                    decision, reason, replay_job_id, created_at
                FROM witness_review WHERE run_id = %s
                ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            return {
                "run_id": str(run_id),
                "project_id": self.project_id,
                "run_version": run["version"],
                "evidence_sha256": evidence_hash,
                "replay_job_id": str(replay["id"]) if replay else None,
                "replay_verified": replay is not None,
                "scope": "SYNTHETIC_ONLY",
                "independent_external_reproduction": False,
                "reviews": [
                    {
                        "review_id": str(row["id"]),
                        "decision": row["decision"],
                        "reason": row["reason"],
                        "reviewer_account_id": str(row["reviewer_account_id"]),
                        "run_version": row["run_version"],
                        "evidence_sha256": row["evidence_sha256"],
                        "replay_job_id": (
                            str(row["replay_job_id"]) if row["replay_job_id"] else None
                        ),
                        "current": (
                            run["status"] == "COMPLETE"
                            and row["run_version"] == run["version"]
                            and row["evidence_sha256"] == evidence_hash
                            and (
                                row["decision"] == "WITHHELD"
                                or (replay is not None
                                    and row["replay_job_id"] == replay["id"])
                            )
                        ),
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in rows
                ],
            }

    def review_witness(
        self, run_id: UUID, *, expected_version: int,
        expected_evidence_sha256: str, decision: str, reason: str,
    ) -> dict:
        """One reviewer-only, immutable synthetic replay decision per version."""
        if (
            self.actor_account_id is None or self.required_role != "reviewer"
        ):
            raise PermissionError("A named independent project reviewer is required")
        if decision not in ("SUPPORTED", "WITHHELD"):
            raise ValueError("Unsupported reviewer decision")
        if not 12 <= len(reason) <= 400:
            raise ValueError("Review reason must be 12-400 characters")
        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT * FROM experiment_run
                WHERE id = %s AND project_id = 'local' FOR UPDATE
                """,
                (run_id,),
            ).fetchone()
            if not run:
                raise ValueError("Run was not found in this project")
            if (
                run["status"] != "COMPLETE" or run["verdict"] != "FAIL"
                or not isinstance(run["witness"], dict)
                or validate_contract(run["witness"]) is not None
                or run["version"] != expected_version
                or run["witness"]["evidence_sha256"] != expected_evidence_sha256
            ):
                raise ValueError("Witness or review version is no longer current")
            replay = self._current_replay(conn, run)
            if decision == "SUPPORTED" and replay is None:
                raise ValueError("A current independently replayed synthetic witness is required")
            existing = conn.execute(
                """
                SELECT id, decision, reason, replay_job_id FROM witness_review
                WHERE run_id = %s AND run_version = %s AND reviewer_account_id = %s
                FOR UPDATE
                """,
                (run_id, expected_version, self.actor_account_id),
            ).fetchone()
            replay_id = replay["id"] if decision == "SUPPORTED" else None
            if existing:
                if (
                    existing["decision"] != decision
                    or existing["reason"] != reason
                    or existing["replay_job_id"] != replay_id
                ):
                    raise ValueError("The review decision is immutable")
                return {"review_id": str(existing["id"]), "recorded": True}
            row = conn.execute(
                """
                INSERT INTO witness_review(
                    id, run_id, run_version, evidence_sha256,
                    reviewer_account_id, decision, reason, replay_job_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    uuid4(), run_id, expected_version, expected_evidence_sha256,
                    self.actor_account_id, decision, reason, replay_id,
                ),
            ).fetchone()
            return {"review_id": str(row["id"]), "recorded": True}

    def transition_page(
        self, run_id: UUID, after: int = 0, limit: int = 100
    ) -> dict:
        """Sequence-ordered pages; a missing SSE notification never loses history."""
        if not 0 <= after <= MAX_EVENT_VERSION or not 1 <= limit <= 100:
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

    def _local_native_active(self, conn) -> int:
        """Count leased native run and verification work in this project schema."""
        row = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM experiment_run
                 WHERE execution_backend = 'native-postgresql'
                   AND status IN ('RUNNING', 'CANCELLING')
                   AND lease_until > now())
              + (SELECT count(*) FROM verification_job
                 WHERE status = 'RUNNING' AND lease_until > now())
                AS occupied
            """
        ).fetchone()
        return row["occupied"]

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
            # Permanently stop recycling an ambiguous claim after three
            # missed receipts. The terminal UNKNOWN is an audited transition
            # in the same transaction that attempts the next claim.
            exhausted = conn.execute(
                """
                SELECT id, execution_backend, step_budget, reserved_cpu_ms
                FROM experiment_run
                WHERE status = 'RUNNING' AND lease_until < now()
                  AND cancel_requested = FALSE
                  AND lease_generation >= %s
                ORDER BY created_at, id
                LIMIT 32 FOR UPDATE SKIP LOCKED
                """,
                (MAX_CLAIM_GENERATIONS,),
            ).fetchall()
            for item in exhausted:
                accounting_doc = uncertain_run_accounting(
                    execution_backend=item["execution_backend"],
                    step_budget=item["step_budget"],
                    reserved_cpu_ms=item["reserved_cpu_ms"],
                    reason=(
                        "Worker lease retry budget exhausted without receipt; "
                        "actual expenditure is uncertain"
                    ),
                )
                terminal = conn.execute(
                    """
                    UPDATE experiment_run
                    SET status = 'UNKNOWN', verdict = 'UNKNOWN',
                        accounting_state = 'UNCERTAIN',
                        resource_accounting = %s,
                        version = version + 1, lease_owner = NULL,
                        lease_until = NULL, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (Jsonb(accounting_doc), item["id"]),
                ).fetchone()
                _event(conn, terminal, "worker receipt missing; retry budget exhausted")
            local_budget_available = (
                self._local_native_active(conn) < quota["max_native_active"]
            )
            row = conn.execute(
                """
                SELECT * FROM experiment_run
                WHERE project_id = 'local'
                  AND (
                    status = 'PENDING'
                    OR (status = 'RUNNING' AND lease_until < now()
                        AND cancel_requested = FALSE
                        AND lease_generation < %s)
                  )
                  AND (
                    execution_backend = 'memory'
                    OR (%s AND %s)
                  )
                ORDER BY created_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (MAX_CLAIM_GENERATIONS, local_budget_available, global_budget_available),
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

    def renew_lease(
        self,
        run_id: UUID,
        owner: str,
        generation: int,
        lease_seconds: int = 30,
    ) -> bool:
        """Extend an active worker claim without mutating lease_generation."""
        if not owner or len(owner) > 100 or not 5 <= lease_seconds <= 300:
            raise ValueError("Invalid worker or lease duration")
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid lease generation")
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE experiment_run
                SET lease_until = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE id = %s AND project_id = 'local'
                  AND status IN ('RUNNING', 'CANCELLING')
                  AND lease_owner = %s
                  AND lease_generation = %s
                  AND lease_until > clock_timestamp()
                RETURNING id
                """,
                (lease_seconds, run_id, owner, generation),
            ).fetchone()
            return row is not None

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
                    <= conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
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

    def mark_target(
        self,
        instance_id: UUID,
        status: str,
        *,
        provision_ms: float | None = None,
        teardown_ms: float | None = None,
    ) -> None:
        """Target records are facts; do not infer run success from cleanup."""
        if status not in ("ACTIVE", "CLEANED", "UNKNOWN"):
            raise ValueError("Invalid target lifecycle status")
        prov, tear = validate_target_latencies(provision_ms, teardown_ms)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE target_instance
                SET status = %s,
                    provision_ms = COALESCE(%s, provision_ms),
                    teardown_ms = COALESCE(%s, teardown_ms),
                    updated_at = now()
                WHERE id = %s AND status != 'CLEANED'
                """,
                (status, prov, tear, instance_id),
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
                SELECT id, lease_generation, status, provision_ms, teardown_ms
                FROM target_instance WHERE run_id = %s
                ORDER BY lease_generation
                """,
                (run_id,),
            ).fetchall()
            return [
                {**row, "id": str(row["id"])}
                for row in rows
            ]

    def finish(
        self,
        run_id: UUID,
        owner: str,
        generation: int,
        witness: dict,
        *,
        measurements: dict | None = None,
    ) -> bool:
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
                or current["lease_until"]
                    <= conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                or current["status"] not in ("RUNNING", "CANCELLING")
            ):
                return False

            valid_witness = validate_contract(witness) is None
            observed = witness.get("observed_steps") if isinstance(witness, dict) else None
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
                not valid_witness
                or witness["oracle"]["sha256"] != current["oracle_hash"]
                or witness.get("variant") != current["variant"]
                or witness.get("schedule") != current["schedule"]
                or witness.get("execution_backend", "memory")
                    != current["execution_backend"]
            ):
                status, verdict, note = "UNKNOWN", "UNKNOWN", "invalid or mismatched witness"
            else:
                verdict = witness.get("verdict")
                if verdict not in ("PASS", "FAIL") or not witness.get("complete"):
                    status, verdict, note = "UNKNOWN", "UNKNOWN", "incomplete evidence"
                else:
                    status, note = "COMPLETE", "independent demo oracle evaluated"
            accounting_state, accounting_doc = reconcile_run_accounting(
                execution_backend=current["execution_backend"],
                step_budget=current["step_budget"],
                reserved_cpu_ms=current["reserved_cpu_ms"],
                terminal_status=status,
                cancel_requested=current["cancel_requested"],
                valid_witness=valid_witness,
                consumed_steps=consumed if valid_witness else None,
                measurements=measurements,
            )
            measured = accounting_doc["measured"]
            updated = conn.execute(
                """
                UPDATE experiment_run SET status = %s, verdict = %s,
                    witness = %s, consumed_steps = %s,
                    accounting_state = %s,
                    measured_worker_cpu_ms = %s,
                    measured_app_wall_ms = %s,
                    measured_db_active_ms = %s,
                    resource_accounting = %s,
                    version = version + 1, lease_owner = NULL,
                    lease_until = NULL, updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (
                    status,
                    verdict,
                    Jsonb(witness) if valid_witness else None,
                    consumed,
                    accounting_state,
                    measured["process"]["worker_cpu_ms"],
                    measured["application"]["wall_ms"],
                    measured["database"]["active_ms"],
                    Jsonb(accounting_doc),
                    run_id,
                ),
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

    def cancel(self, run_id: UUID, expected_version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_run WHERE id = %s AND project_id = 'local' FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            if row["version"] != expected_version:
                # A retry of the cancellation accepted at expected_version is
                # idempotent; any other stale request must not mutate the run.
                accepted = conn.execute(
                    """
                    SELECT 1 FROM run_transition
                    WHERE run_id = %s AND version = %s
                      AND note = 'operator requested cancellation'
                    """,
                    (run_id, expected_version + 1),
                ).fetchone()
                if accepted:
                    return _public(row, self.project_id)
                raise ValueError("Run changed; refresh before cancelling")
            if row["status"] in ("PENDING", "RUNNING"):
                if row["status"] == "PENDING":
                    status = "CANCELLED"
                    accounting_state = "RELEASED"
                    accounting_doc = released_run_accounting(
                        execution_backend=row["execution_backend"],
                        step_budget=row["step_budget"],
                        reserved_cpu_ms=row["reserved_cpu_ms"],
                    )
                    row = conn.execute(
                        """
                        UPDATE experiment_run SET
                            cancel_requested = TRUE, status = %s,
                            accounting_state = %s, resource_accounting = %s,
                            version = version + 1, updated_at = now()
                        WHERE id = %s RETURNING *
                        """,
                        (status, accounting_state, Jsonb(accounting_doc), run_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        UPDATE experiment_run SET
                            cancel_requested = TRUE, status = 'CANCELLING',
                            version = version + 1, updated_at = now()
                        WHERE id = %s RETURNING *
                        """,
                        (run_id,),
                    ).fetchone()
                _event(conn, row, "operator requested cancellation")
            return _public(row, self.project_id)

    def reconcile_expired_cancelled(self) -> int:
        """Reconcile one bounded batch of expired in-flight cancellations."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, execution_backend, step_budget, reserved_cpu_ms
                FROM experiment_run
                WHERE status = 'CANCELLING' AND lease_until < now()
                ORDER BY lease_until, created_at, id
                LIMIT 32 FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for item in rows:
                accounting_doc = uncertain_run_accounting(
                    execution_backend=item["execution_backend"],
                    step_budget=item["step_budget"],
                    reserved_cpu_ms=item["reserved_cpu_ms"],
                    reason=(
                        "Cancelled claim expired before reconciliation; "
                        "actual expenditure is uncertain"
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE experiment_run SET status = 'UNKNOWN', verdict = 'UNKNOWN',
                        accounting_state = 'UNCERTAIN', resource_accounting = %s,
                        version = version + 1, lease_owner = NULL, lease_until = NULL,
                        updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (Jsonb(accounting_doc), item["id"]),
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
