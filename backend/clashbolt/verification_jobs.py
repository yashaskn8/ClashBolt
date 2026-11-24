"""Version-fenced native replay/reduction work for a separate trusted worker.

The API only queues allowlisted synthetic jobs. Target-cluster credentials are
held by the worker and never returned to the browser or stored in this table.
"""

import os
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.store import GLOBAL_NATIVE_LOCK_ID, GLOBAL_NATIVE_SLOTS, Ledger

KINDS = ("REPLAY", "REDUCE")


def _public(row: dict) -> dict:
    return {
        "job_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "run_version": row["run_version"],
        "kind": row["kind"],
        "status": row["status"],
        "result": row["result"],
        "created_at": row["created_at"].isoformat(),
    }


class VerificationJobs:
    def __init__(
        self, dsn: str | Ledger, *, schema: str = "public", project_id: str = "local"
    ):
        self.ledger = (
            dsn if isinstance(dsn, Ledger) else Ledger(dsn, schema=schema, project_id=project_id)
        )

    def submit(self, run_id: UUID, kind: str) -> dict:
        if kind not in KINDS:
            raise ValueError("Unknown verification action")
        with self.ledger.connect() as conn:
            run = conn.execute(
                """
                SELECT id, version, status, verdict, witness
                FROM experiment_run WHERE id = %s AND project_id = 'local'
                FOR SHARE
                """,
                (run_id,),
            ).fetchone()
            if (
                not run or run["status"] != "COMPLETE"
                or run["verdict"] not in ("PASS", "FAIL")
                or run["witness"] is None
                or (kind == "REDUCE" and run["verdict"] != "FAIL")
            ):
                raise ValueError("Current certified witness is required")
            conn.execute(
                """
                INSERT INTO verification_job(id, run_id, run_version, kind)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id, run_version, kind) DO NOTHING
                """,
                (uuid4(), run_id, run["version"], kind),
            )
            job = conn.execute(
                """
                SELECT * FROM verification_job
                WHERE run_id = %s AND run_version = %s AND kind = %s
                """,
                (run_id, run["version"], kind),
            ).fetchone()
            return _public(job)

    def claim(self, owner: str, lease_seconds: int = 30) -> dict | None:
        if not 1 <= len(owner) <= 100 or not 5 <= lease_seconds <= 300:
            raise ValueError("Invalid verification worker or lease duration")
        with self.ledger.connect() as conn:
            if os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts":
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (GLOBAL_NATIVE_LOCK_ID,)
                )
            conn.execute(
                """
                UPDATE verification_job
                SET status = 'UNKNOWN', updated_at = now(),
                    result = %s
                WHERE status = 'RUNNING' AND lease_until < now()
                """,
                (Jsonb({
                    "status": "UNCONFIRMED",
                    "reason": "Worker receipt was missing after lease expiry",
                }),),
            )
            if (
                os.getenv("CLASHBOLT_ACCESS_MODE") == "accounts"
                and self.ledger._global_native_active(conn) >= GLOBAL_NATIVE_SLOTS
            ):
                return None
            # The project limit is shared by native executions and all
            # verification claims. Lock the same quota row as Ledger.claim,
            # after the global advisory lock, to serialize mixed workers.
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') "
                "ON CONFLICT DO NOTHING"
            )
            quota = conn.execute(
                "SELECT max_native_active FROM project_quota "
                "WHERE project_id = 'local' FOR UPDATE"
            ).fetchone()
            if self.ledger._local_native_active(conn) >= quota["max_native_active"]:
                return None
            job = conn.execute(
                """
                SELECT * FROM verification_job
                WHERE status = 'PENDING'
                ORDER BY created_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if not job:
                return None
            row = conn.execute(
                """
                UPDATE verification_job
                SET status = 'RUNNING', lease_owner = %s,
                    lease_generation = lease_generation + 1,
                    lease_until = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (owner, lease_seconds, job["id"]),
            ).fetchone()
            return {**_public(row), "lease_generation": row["lease_generation"]}

    def track_target(
        self, job_id: UUID, owner: str, generation: int, instance_id: UUID
    ) -> bool:
        """Persist target identity before provisioning so crashed workers can be cleaned."""
        with self.ledger.connect() as conn:
            current = conn.execute(
                """
                SELECT lease_until FROM verification_job
                WHERE id = %s AND status = 'RUNNING'
                  AND lease_owner = %s AND lease_generation = %s
                FOR SHARE
                """,
                (job_id, owner, generation),
            ).fetchone()
            if (
                not current or current["lease_until"] is None
                or current["lease_until"]
                    <= conn.execute("SELECT clock_timestamp() AS clock").fetchone()["clock"]
            ):
                return False
            conn.execute(
                """
                INSERT INTO verification_target_instance(
                    id, job_id, lease_generation, status
                ) VALUES (%s, %s, %s, 'PROVISIONING')
                """,
                (instance_id, job_id, generation),
            )
            return True

    def mark_target(self, instance_id: UUID, status: str) -> None:
        if status not in ("ACTIVE", "CLEANED", "UNKNOWN"):
            raise ValueError("Invalid verification target state")
        with self.ledger.connect() as conn:
            conn.execute(
                """
                UPDATE verification_target_instance
                SET status = %s, updated_at = now()
                WHERE id = %s AND status != 'CLEANED'
                """,
                (status, instance_id),
            )

    def claim_abandoned_targets(self, limit: int = 8) -> list[UUID]:
        """Claim only targets whose verification lease is no longer live."""
        if not 1 <= limit <= 50:
            raise ValueError("Invalid verification target cleanup limit")
        with self.ledger.connect() as conn:
            targets = conn.execute(
                """
                SELECT t.id FROM verification_target_instance t
                JOIN verification_job j ON j.id = t.job_id
                WHERE t.status IN (
                    'PROVISIONING', 'ACTIVE', 'UNKNOWN', 'CLEANING'
                )
                  AND t.updated_at < now() - interval '5 seconds'
                  AND (
                      j.status != 'RUNNING'
                      OR j.lease_generation != t.lease_generation
                      OR j.lease_until < now()
                  )
                ORDER BY t.created_at, t.id
                LIMIT %s FOR UPDATE OF t SKIP LOCKED
                """,
                (limit,),
            ).fetchall()
            for target in targets:
                conn.execute(
                    """
                    UPDATE verification_target_instance
                    SET status = 'CLEANING', updated_at = now()
                    WHERE id = %s
                    """,
                    (target["id"],),
                )
            return [row["id"] for row in targets]

    def finish(
        self, job_id: UUID, owner: str, generation: int, report: dict
    ) -> bool:
        with self.ledger.connect() as conn:
            job = conn.execute(
                "SELECT * FROM verification_job WHERE id = %s FOR UPDATE",
                (job_id,),
            ).fetchone()
            if (
                not job or job["status"] != "RUNNING"
                or job["lease_owner"] != owner
                or job["lease_generation"] != generation
                or job["lease_until"] is None
            ):
                return False
            run = conn.execute(
                """
                SELECT status, verdict, version, witness FROM experiment_run
                WHERE id = %s AND project_id = 'local' FOR SHARE
                """,
                (job["run_id"],),
            ).fetchone()
            if job["lease_until"] <= conn.execute(
                "SELECT clock_timestamp() AS clock"
            ).fetchone()["clock"]:
                return False
            current = bool(
                run and run["status"] == "COMPLETE"
                and run["version"] == job["run_version"]
                and run["verdict"] in ("PASS", "FAIL")
                and run["witness"]
                and (job["kind"] != "REDUCE" or run["verdict"] == "FAIL")
            )
            credible = False
            if current:
                evidence_hash = run["witness"]["evidence_sha256"]
                if job["kind"] == "REPLAY":
                    credible = (
                        report.get("status") == "REPRODUCED"
                        and report.get("scope") == "SYNTHETIC_ONLY"
                        and report.get("original_sha256") == evidence_hash
                        and report.get("replay_sha256") == evidence_hash
                        and report.get("verdict") == run["verdict"]
                    )
                elif job["kind"] == "REDUCE":
                    credible = (
                        report.get("status") in ("MINIMAL", "BOUNDED")
                        and report.get("scope") == "SYNTHETIC_ONLY"
                        and report.get("original_evidence_sha256") == evidence_hash
                        and isinstance(report.get("minimal_gate_count"), int)
                        and 1 <= report["minimal_gate_count"] <= 4
                    )
            if current and credible:
                final = report
            else:
                final = {
                    "status": "UNCONFIRMED",
                    "reason": (
                        "Verification report did not match approved evidence"
                        if current
                        else "Certified run changed while verification was in flight"
                    ),
                }
            conn.execute(
                """
                UPDATE verification_job
                SET status = %s, result = %s,
                    updated_at = now(), lease_until = NULL
                WHERE id = %s
                """,
                (
                    "COMPLETE" if current and credible else "UNKNOWN",
                    Jsonb(final), job_id,
                ),
            )
            return True

    def get(self, job_id: UUID) -> dict | None:
        with self.ledger.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, r.version AS current_version,
                    r.status AS run_status,
                    (j.status = 'RUNNING' AND j.lease_until < now()) AS lease_expired
                FROM verification_job j
                JOIN experiment_run r ON r.id = j.run_id
                WHERE j.id = %s AND r.project_id = 'local'
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            result = _public(row)
            if (
                row["current_version"] != row["run_version"]
                or row["run_status"] != "COMPLETE"
            ):
                result["status"] = "UNKNOWN"
                result["result"] = {
                    "status": "UNCONFIRMED",
                    "reason": "The oracle adjudication has changed",
                }
            elif row["lease_expired"]:
                # A timed-out claimant has no receipt authority even before
                # the next worker atomically persists the UNKNOWN transition.
                result["status"] = "UNKNOWN"
                result["result"] = {
                    "status": "UNCONFIRMED",
                    "reason": "Worker receipt was missing after lease expiry",
                }
            return result
