"""Version-fenced native replay/reduction work for a separate trusted worker.

The API only queues allowlisted synthetic jobs. Target-cluster credentials are
held by the worker and never returned to the browser or stored in this table.
"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.store import Ledger

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
    def __init__(self, dsn: str):
        self.ledger = Ledger(dsn)

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
                or job["lease_until"]
                    <= conn.execute("SELECT now() AS clock").fetchone()["clock"]
            ):
                return False
            run = conn.execute(
                """
                SELECT status, verdict, version, witness FROM experiment_run
                WHERE id = %s AND project_id = 'local' FOR SHARE
                """,
                (job["run_id"],),
            ).fetchone()
            current = bool(
                run and run["status"] == "COMPLETE"
                and run["version"] == job["run_version"]
                and run["verdict"] in ("PASS", "FAIL")
                and run["witness"]
                and (job["kind"] != "REDUCE" or run["verdict"] == "FAIL")
            )
            credible = (
                (job["kind"] == "REPLAY" and report.get("status") == "REPRODUCED")
                or (
                    job["kind"] == "REDUCE"
                    and report.get("status") in ("MINIMAL", "BOUNDED")
                    and report.get("original_evidence_sha256")
                        == run["witness"]["evidence_sha256"] if current else False
                )
            )
            final = report if current else {
                "status": "UNCONFIRMED",
                "reason": "Certified run changed while verification was in flight",
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
                    r.status AS run_status
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
            return result
