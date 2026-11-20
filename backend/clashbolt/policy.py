"""Operator-approved demo oracles and one-use scoped tool capabilities.

Agent output can propose a plan but cannot approve the oracle or issue authority.
All tool access is local and limited to the shipped synthetic inventory fixture.
"""

import hashlib
import secrets
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.experiment import (
    ORACLE_HASH,
    ORACLE_RULE,
    ORACLE_VERSION,
    SCHEDULES,
    TARGET_ID,
    TARGET_REVISION,
)
from clashbolt.store import Ledger

_POLICY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS oracle_proposal (
        id UUID PRIMARY KEY,
        target_revision TEXT NOT NULL,
        rule_hash TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        description TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('PROPOSED', 'APPROVED', 'REVOKED')),
        proposed_by TEXT NOT NULL,
        reviewed_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reviewed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_capability (
        id UUID PRIMARY KEY,
        token_hash TEXT UNIQUE NOT NULL,
        proposal_id UUID NOT NULL REFERENCES oracle_proposal(id),
        target_revision TEXT NOT NULL,
        variant TEXT NOT NULL CHECK (variant IN ('racy', 'locked')),
        schedule TEXT NOT NULL CHECK (schedule IN ('overlap', 'serial')),
        max_calls INTEGER NOT NULL CHECK (max_calls BETWEEN 1 AND 10),
        used_calls INTEGER NOT NULL DEFAULT 0,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_audit (
        id BIGSERIAL PRIMARY KEY,
        capability_id UUID REFERENCES tool_capability(id),
        variant TEXT NOT NULL,
        schedule TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('ALLOW', 'DENY')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_plan (
        id UUID PRIMARY KEY,
        proposal_id UUID NOT NULL REFERENCES oracle_proposal(id),
        plan JSONB NOT NULL,
        strategy TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'PROPOSED',
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE tool_capability ADD COLUMN IF NOT EXISTS plan_id UUID "
    "REFERENCES agent_plan(id)",
    "ALTER TABLE tool_audit ADD COLUMN IF NOT EXISTS plan_id UUID "
    "REFERENCES agent_plan(id)",
)


class Policy(Ledger):
    def migrate(self) -> None:
        super().migrate()
        with self.connect() as conn:
            for statement in _POLICY_SCHEMA:
                conn.execute(statement)

    def propose(self, proposed_by: str = "deterministic-workshop") -> dict:
        """Create a candidate for the sole shipped, non-executable demo rule."""
        if not proposed_by or len(proposed_by) > 100:
            raise ValueError("Invalid proposal source")
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO oracle_proposal (
                    id, target_revision, rule_hash, rule_version,
                    description, state, proposed_by
                )
                VALUES (%s, %s, %s, %s, %s, 'PROPOSED', %s)
                RETURNING id, rule_hash, rule_version, state, target_revision
                """,
                (uuid4(), TARGET_REVISION, ORACLE_HASH, ORACLE_VERSION, ORACLE_RULE, proposed_by),
            ).fetchone()
            return {**row, "id": str(row["id"]), "target_id": TARGET_ID}

    def list_proposals(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, rule_hash, rule_version, target_revision, description,
                       state, proposed_by, reviewed_by
                FROM oracle_proposal ORDER BY created_at DESC LIMIT 100
                """
            ).fetchall()
            return [{**row, "id": str(row["id"])} for row in rows]

    def approve(self, proposal_id: UUID, expected_hash: str, reviewer: str) -> dict:
        if not reviewer or len(reviewer) > 100:
            raise ValueError("Reviewer identity is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oracle_proposal WHERE id = %s FOR UPDATE", (proposal_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "PROPOSED"
                or row["target_revision"] != TARGET_REVISION
                or row["rule_hash"] != ORACLE_HASH
                or expected_hash != row["rule_hash"]
            ):
                raise ValueError("Stale, missing, or non-matching oracle version")
            if row["proposed_by"] == reviewer:
                raise ValueError("A proposer cannot approve their own rule")
            approved = conn.execute(
                """
                UPDATE oracle_proposal
                SET state = 'APPROVED', reviewed_by = %s, reviewed_at = now()
                WHERE id = %s
                RETURNING id, rule_hash, state, reviewed_by
                """,
                (reviewer, proposal_id),
            ).fetchone()
            return {**approved, "id": str(approved["id"])}

    def revoke_rule(self, proposal_id: UUID) -> bool:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE oracle_proposal SET state = 'REVOKED'
                WHERE id = %s AND state = 'APPROVED' RETURNING id
                """,
                (proposal_id,),
            ).fetchone()
            return changed is not None

    def issue(
        self,
        proposal_id: UUID,
        variant: str,
        schedule: str,
        max_calls: int = 1,
        lifetime_seconds: int = 600,
        plan_id: UUID | None = None,
    ) -> dict:
        if variant not in ("racy", "locked") or schedule not in SCHEDULES:
            raise ValueError("Unsupported fixture action")
        if not 1 <= max_calls <= 10 or not 60 <= lifetime_seconds <= 3600:
            raise ValueError("Tool budget or lifetime exceeds allowed limits")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT * FROM oracle_proposal WHERE id = %s FOR UPDATE", (proposal_id,)
            ).fetchone()
            if (
                not rule
                or rule["state"] != "APPROVED"
                or rule["rule_hash"] != ORACLE_HASH
                or rule["target_revision"] != TARGET_REVISION
            ):
                raise ValueError("Tool requires a currently approved oracle version")
            if plan_id is not None:
                planned = conn.execute(
                    "SELECT * FROM agent_plan WHERE id = %s AND proposal_id = %s FOR UPDATE",
                    (plan_id, proposal_id),
                ).fetchone()
                if (
                    not planned
                    or planned["state"] != "PROPOSED"
                    or planned["plan"].get("variant") != variant
                    or planned["plan"].get("schedule") != schedule
                    or max_calls != 1
                ):
                    raise ValueError("Capability must match one pending approved plan")
            cap = conn.execute(
                """
                INSERT INTO tool_capability (
                    id, token_hash, proposal_id, target_revision, variant, schedule,
                    max_calls, expires_at, plan_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    now() + (%s * interval '1 second'), %s
                )
                RETURNING id, max_calls, expires_at
                """,
                (
                    uuid4(), token_hash, proposal_id, TARGET_REVISION,
                    variant, schedule, max_calls, lifetime_seconds, plan_id,
                ),
            ).fetchone()
            # The secret is returned exactly once, never stored or recorded in audit.
            return {
                "capability_id": str(cap["id"]),
                "token": token,
                "max_calls": cap["max_calls"],
                "expires_at": cap["expires_at"].isoformat(),
                "target_revision": TARGET_REVISION,
                "variant": variant,
                "schedule": schedule,
                "plan_id": str(plan_id) if plan_id else None,
            }

    def revoke_capability(self, capability_id: UUID) -> bool:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE tool_capability SET revoked = TRUE
                WHERE id = %s AND revoked = FALSE RETURNING id
                """,
                (capability_id,),
            ).fetchone()
            return changed is not None

    def authorize(
        self, token: str, variant: str, schedule: str, plan_id: UUID | None = None
    ) -> bool:
        """Validate+debit token atomically before any tool action is dispatched."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, p.state AS oracle_state, p.rule_hash
                FROM tool_capability c
                JOIN oracle_proposal p ON p.id = c.proposal_id
                WHERE c.token_hash = %s FOR UPDATE OF c
                """,
                (token_hash,),
            ).fetchone()
            plan_ok = plan_id is None
            if row and row["plan_id"] is not None and plan_id == row["plan_id"]:
                planned = conn.execute(
                    "SELECT * FROM agent_plan WHERE id = %s FOR UPDATE", (plan_id,)
                ).fetchone()
                plan_ok = bool(
                    planned
                    and planned["state"] == "PROPOSED"
                    and planned["proposal_id"] == row["proposal_id"]
                    and planned["plan"].get("variant") == variant
                    and planned["plan"].get("schedule") == schedule
                )
            elif row and row["plan_id"] is not None:
                plan_ok = False
            allowed = bool(
                row
                and plan_ok
                and row["oracle_state"] == "APPROVED"
                and row["rule_hash"] == ORACLE_HASH
                and row["target_revision"] == TARGET_REVISION
                and not row["revoked"]
                and row["used_calls"] < row["max_calls"]
                and row["expires_at"] > conn.execute("SELECT now() AS now").fetchone()["now"]
                and row["variant"] == variant
                and row["schedule"] == schedule
            )
            conn.execute(
                """
                INSERT INTO tool_audit(
                    capability_id, variant, schedule, decision, plan_id
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    row["id"] if row else None,
                    variant, schedule, "ALLOW" if allowed else "DENY",
                    plan_id if row and row["plan_id"] == plan_id else None,
                ),
            )
            if allowed:
                conn.execute(
                    "UPDATE tool_capability SET used_calls = used_calls + 1 WHERE id = %s",
                    (row["id"],),
                )
                if plan_id is not None:
                    conn.execute(
                        "UPDATE agent_plan SET state = 'IN_FLIGHT' WHERE id = %s",
                        (plan_id,),
                    )
            return allowed

    def record_plan(self, proposal_id: UUID, plan: dict) -> dict:
        """Persist typed deterministic planning intent; no tool call is implied."""
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                raise ValueError("Only approved oracle versions may be planned")
            row = conn.execute(
                """
                INSERT INTO agent_plan(id, proposal_id, plan, strategy)
                VALUES (%s, %s, %s, %s)
                RETURNING id, strategy, state
                """,
                (uuid4(), proposal_id, Jsonb(plan), plan["strategy"]),
            ).fetchone()
            return {**row, "id": str(row["id"]), "plan": plan}

    def get_plan(self, plan_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, proposal_id, plan, state, result FROM agent_plan WHERE id = %s",
                (plan_id,),
            ).fetchone()
            if not row:
                return None
            return {
                **row,
                "id": str(row["id"]),
                "proposal_id": str(row["proposal_id"]),
            }

    def record_outcome(self, plan_id: UUID, witness: dict) -> bool:
        """Write a single plan observation after independent contract validation."""
        from clashbolt.witness import validate_contract

        invalid = validate_contract(witness)
        final_state = "UNKNOWN" if invalid else "COMPLETE"
        receipt = {
            "verdict": witness.get("verdict") if not invalid else "UNKNOWN",
            "evidence_sha256": witness.get("evidence_sha256") if not invalid else None,
            "evidence_gap": invalid,
        }
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE agent_plan SET state = %s, result = %s
                WHERE id = %s AND state = 'IN_FLIGHT' RETURNING id
                """,
                (final_state, Jsonb(receipt), plan_id),
            ).fetchone()
            return updated is not None

    def audit(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT capability_id, plan_id, variant, schedule, decision, created_at
                FROM tool_audit ORDER BY id DESC LIMIT %s
                """,
                (min(max(limit, 1), 100),),
            ).fetchall()
            return [
                {
                    **row,
                    "capability_id": str(row["capability_id"]) if row["capability_id"] else None,
                    "plan_id": str(row["plan_id"]) if row["plan_id"] else None,
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]

if __name__ == "__main__":
    import os

    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        raise SystemExit("Set CLASHBOLT_DATABASE_URL for the local PostgreSQL database")
    Policy(dsn).migrate()
    print("ClashBolt rule and capability schema ready")
