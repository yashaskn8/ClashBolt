"""Operator-approved demo oracles and one-use scoped tool capabilities.

Agent output can propose a plan but cannot approve the oracle or issue authority.
All tool access is local and limited to the shipped synthetic inventory fixture.
"""

import hashlib
import secrets
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.agent_feedback import decide_feedback, gap_class
from clashbolt.experiment import (
    ORACLE_HASH,
    ORACLE_RULE,
    ORACLE_VERSION,
    SCHEDULES,
    TARGET_ID,
    TARGET_REVISION,
)
from clashbolt.frontier import next_frontier_plan, rank_frontier
from clashbolt.oracle_workshop import RuleCandidate, evaluate_candidate, valid_evidence
from clashbolt.store import SCHEMA_LOCK_ID, Ledger, _record_schema_revision

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
    "ALTER TABLE oracle_proposal ADD COLUMN IF NOT EXISTS evidence JSONB",
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_oracle_version()
    RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
    BEGIN
        IF OLD.state = 'REVOKED' THEN
            RAISE EXCEPTION 'Revoked oracle versions are immutable';
        END IF;
        IF NEW.target_revision IS DISTINCT FROM OLD.target_revision
            OR NEW.rule_hash IS DISTINCT FROM OLD.rule_hash
            OR NEW.rule_version IS DISTINCT FROM OLD.rule_version
            OR NEW.description IS DISTINCT FROM OLD.description
            OR NEW.proposed_by IS DISTINCT FROM OLD.proposed_by
            OR (
                OLD.state != 'PROPOSED'
                AND NEW.evidence IS DISTINCT FROM OLD.evidence
            )
        THEN
            RAISE EXCEPTION 'Approved oracle rule identity cannot change';
        END IF;
        IF OLD.state = 'PROPOSED' THEN
            IF NEW.state NOT IN ('PROPOSED', 'APPROVED') THEN
                RAISE EXCEPTION 'Invalid oracle state transition';
            END IF;
            IF NEW.state = 'APPROVED' AND (
                NEW.evidence IS DISTINCT FROM OLD.evidence
                OR NEW.reviewed_by IS NULL OR NEW.reviewed_at IS NULL
            ) THEN
                RAISE EXCEPTION 'Approved oracle must bind existing evidence and reviewer';
            END IF;
        ELSIF OLD.state = 'APPROVED' THEN
            IF NEW.state != 'REVOKED'
                OR NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
                OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
            THEN
                RAISE EXCEPTION 'Approved oracle may only be revoked';
            END IF;
        END IF;
        RETURN NEW;
    END
    $guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_oracle_immutable ON oracle_proposal",
    """
    CREATE TRIGGER clashbolt_oracle_immutable
    BEFORE UPDATE ON oracle_proposal FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_oracle_version()
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
    "ALTER TABLE experiment_run ADD COLUMN IF NOT EXISTS approved_oracle_id "
    "UUID REFERENCES oracle_proposal(id)",
    "ALTER TABLE tool_capability ADD COLUMN IF NOT EXISTS plan_id UUID "
    "REFERENCES agent_plan(id)",
    "ALTER TABLE tool_audit ADD COLUMN IF NOT EXISTS plan_id UUID "
    "REFERENCES agent_plan(id)",
    "ALTER TABLE agent_plan ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
    "ALTER TABLE agent_plan ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS model_budget (
        project_id TEXT NOT NULL REFERENCES project_quota(project_id),
        window_date DATE NOT NULL DEFAULT CURRENT_DATE,
        call_limit INTEGER NOT NULL DEFAULT 20 CHECK (call_limit BETWEEN 1 AND 1000),
        token_limit INTEGER NOT NULL DEFAULT 5120 CHECK (token_limit BETWEEN 256 AND 1000000),
        calls_reserved INTEGER NOT NULL DEFAULT 0,
        tokens_reserved INTEGER NOT NULL DEFAULT 0,
        tokens_observed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, window_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_reservation (
        id UUID PRIMARY KEY,
        project_id TEXT NOT NULL,
        window_date DATE NOT NULL,
        reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens BETWEEN 1 AND 256),
        observed_tokens INTEGER,
        state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMPLETE', 'UNKNOWN')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (project_id, window_date)
            REFERENCES model_budget(project_id, window_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_evidence_gap (
        plan_id UUID PRIMARY KEY REFERENCES agent_plan(id),
        classification TEXT NOT NULL CHECK (
            classification IN (
                'MISSING_RECEIPT', 'WITHDRAWN_ORACLE',
                'INCOMPLETE_OBSERVATION', 'CONFLICTING_EVIDENCE'
            )
        ),
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_feedback (
        plan_id UUID PRIMARY KEY REFERENCES agent_plan(id),
        oracle_id UUID NOT NULL REFERENCES oracle_proposal(id),
        evidence_sha256 TEXT,
        policy_version TEXT NOT NULL,
        remaining_budget INTEGER NOT NULL CHECK (remaining_budget BETWEEN 0 AND 32),
        decision JSONB NOT NULL,
        next_plan_id UUID REFERENCES agent_plan(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


POLICY_SCHEMA_VERSION = 5


class Policy(Ledger):
    def migrate(self) -> None:
        """Control and policy schema changes commit atomically as one version."""
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            self._apply_base_schema(conn)
            for statement in _POLICY_SCHEMA:
                conn.execute(statement)
            _record_schema_revision(conn, "policy", POLICY_SCHEMA_VERSION)

    def reserve_model_call(self) -> UUID:
        """Atomically reserve a worst-case 256-token local request before HTTP."""
        reservation_id = uuid4()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            conn.execute(
                """
                INSERT INTO model_budget(project_id, window_date)
                VALUES ('local', CURRENT_DATE) ON CONFLICT DO NOTHING
                """
            )
            budget = conn.execute(
                """
                SELECT * FROM model_budget
                WHERE project_id = 'local' AND window_date = CURRENT_DATE
                FOR UPDATE
                """
            ).fetchone()
            if (
                budget["calls_reserved"] >= budget["call_limit"]
                or budget["tokens_reserved"] + 256 > budget["token_limit"]
            ):
                raise ValueError("Local model daily reservation budget exhausted")
            conn.execute(
                """
                UPDATE model_budget SET calls_reserved = calls_reserved + 1,
                    tokens_reserved = tokens_reserved + 256
                WHERE project_id = 'local' AND window_date = CURRENT_DATE
                """
            )
            conn.execute(
                """
                INSERT INTO model_reservation(
                    id, project_id, window_date, reserved_tokens, state
                ) VALUES (%s, 'local', CURRENT_DATE, 256, 'RESERVED')
                """,
                (reservation_id,),
            )
        return reservation_id

    def settle_model_call(
        self, reservation_id: UUID, observed_tokens: int | None
    ) -> bool:
        """Unknown outcomes consume the full reservation; never guess provider cost."""
        if observed_tokens is not None and (
            type(observed_tokens) is not int or not 0 <= observed_tokens <= 256
        ):
            raise ValueError("Reported model token usage exceeds reserved amount")
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE model_reservation
                SET observed_tokens = %s,
                    state = %s
                WHERE id = %s AND state = 'RESERVED'
                RETURNING project_id, window_date
                """,
                (
                    observed_tokens,
                    "UNKNOWN" if observed_tokens is None else "COMPLETE",
                    reservation_id,
                ),
            ).fetchone()
            if not row:
                return False
            if observed_tokens is not None:
                conn.execute(
                    """
                    UPDATE model_budget SET tokens_observed = tokens_observed + %s
                    WHERE project_id = %s AND window_date = %s
                    """,
                    (observed_tokens, row["project_id"], row["window_date"]),
                )
            return True

    def local_model_budget(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT call_limit, token_limit, calls_reserved, tokens_reserved,
                    tokens_observed, window_date
                FROM model_budget
                WHERE project_id = 'local' AND window_date = CURRENT_DATE
                """
            ).fetchone()
            if not row:
                return {
                    "calls_reserved": 0, "tokens_reserved": 0, "tokens_observed": 0,
                    "call_limit": 20, "token_limit": 5120,
                }
            return {**row, "window_date": row["window_date"].isoformat()}

    def propose(
        self, proposed_by: str = "deterministic-workshop",
        candidate: RuleCandidate | None = None,
    ) -> dict:
        """Store a typed proposal and fixed-checker counterexample evidence."""
        if not proposed_by or len(proposed_by) > 100:
            raise ValueError("Invalid proposal source")
        evidence = evaluate_candidate(candidate or RuleCandidate())
        if not valid_evidence(evidence):
            raise ValueError("Candidate failed deterministic fixture checks")
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO oracle_proposal (
                    id, target_revision, rule_hash, rule_version,
                    description, state, proposed_by, evidence
                )
                VALUES (%s, %s, %s, %s, %s, 'PROPOSED', %s, %s)
                RETURNING id, rule_hash, rule_version, state, target_revision, evidence
                """,
                (
                    uuid4(), TARGET_REVISION, ORACLE_HASH, ORACLE_VERSION,
                    ORACLE_RULE, proposed_by, Jsonb(evidence),
                ),
            ).fetchone()
            return {**row, "id": str(row["id"]), "target_id": TARGET_ID}

    def list_proposals(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, rule_hash, rule_version, target_revision, description,
                       state, proposed_by, reviewed_by, evidence
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
                or not valid_evidence(row["evidence"])
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
            rule = conn.execute(
                "SELECT state FROM oracle_proposal WHERE id = %s FOR UPDATE",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED":
                return False
            conn.execute(
                "UPDATE oracle_proposal SET state = 'REVOKED' WHERE id = %s",
                (proposal_id,),
            )
            accepted = conn.execute(
                """
                SELECT id FROM experiment_run
                WHERE approved_oracle_id = %s AND status = 'COMPLETE'
                ORDER BY id FOR UPDATE
                """,
                (proposal_id,),
            ).fetchall()
            for item in accepted:
                row = conn.execute(
                    """
                    UPDATE experiment_run SET
                        status = 'UNKNOWN', verdict = 'UNKNOWN',
                        version = version + 1, updated_at = now()
                    WHERE id = %s RETURNING id, version, status
                    """,
                    (item["id"],),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO run_transition(run_id, version, status, note)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        row["id"], row["version"], row["status"],
                        "oracle approval revoked; prior verdict withdrawn",
                    ),
                )
            completed_plans = conn.execute(
                """
                SELECT id, result FROM agent_plan
                WHERE proposal_id = %s AND state = 'COMPLETE'
                ORDER BY id FOR UPDATE
                """,
                (proposal_id,),
            ).fetchall()
            for plan in completed_plans:
                receipt = plan["result"] or {}
                conn.execute(
                    """
                    UPDATE agent_plan SET state = 'UNKNOWN', result = %s
                    WHERE id = %s
                    """,
                    (
                        Jsonb({
                            "verdict": "UNKNOWN",
                            "evidence_sha256": None,
                            "historic_evidence_sha256": receipt.get("evidence_sha256"),
                            "evidence_gap": "Oracle approval revoked",
                        }),
                        plan["id"],
                    ),
                )
                self._record_gap(conn, plan["id"], "Oracle approval revoked")
            return True

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
                        """
                        UPDATE agent_plan SET state = 'IN_FLIGHT',
                            started_at = now(),
                            deadline_at = now() + interval '30 seconds'
                        WHERE id = %s AND state = 'PROPOSED'
                        """,
                        (plan_id,),
                    )
            return allowed

    def frontier(self, proposal_id: UUID, variant: str) -> dict:
        if variant not in ("racy", "locked"):
            raise ValueError("Unsupported synthetic fixture variant")
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                raise ValueError("Frontier requires an approved oracle")
            evidence = conn.execute(
                """
                SELECT plan, result FROM agent_plan
                WHERE proposal_id = %s AND state IN ('COMPLETE', 'UNKNOWN')
                ORDER BY created_at, id LIMIT 1000
                """,
                (proposal_id,),
            ).fetchall()
            return {
                "proposal_id": str(proposal_id),
                "scope": "SHIPPED_SYNTHETIC_FIXTURE_ONLY",
                "policy_version": "known-inventory-frontier-v1",
                "variant": variant,
                "candidates": rank_frontier(evidence, variant),
                "promotion_eligible": False,
            }

    def propose_frontier_plan(self, proposal_id: UUID, variant: str) -> dict:
        if variant not in ("racy", "locked"):
            raise ValueError("Unsupported synthetic fixture variant")
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s FOR SHARE",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                raise ValueError("Frontier requires an approved oracle")
            evidence = conn.execute(
                """
                SELECT plan, result FROM agent_plan
                WHERE proposal_id = %s AND state IN ('COMPLETE', 'UNKNOWN')
                ORDER BY created_at, id LIMIT 1000
                """,
                (proposal_id,),
            ).fetchall()
            plan = next_frontier_plan(evidence, variant)
            row = conn.execute(
                """
                INSERT INTO agent_plan(id, proposal_id, plan, strategy)
                VALUES (%s, %s, %s, %s)
                RETURNING id, strategy, state
                """,
                (uuid4(), proposal_id, Jsonb(plan), plan["strategy"]),
            ).fetchone()
            return {**row, "id": str(row["id"]), "plan": plan}

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

    @staticmethod
    def _record_gap(conn, plan_id: UUID, reason: str) -> None:
        conn.execute(
            """
            INSERT INTO agent_evidence_gap(plan_id, classification, reason)
            VALUES (%s, %s, %s)
            ON CONFLICT (plan_id) DO NOTHING
            """,
            (plan_id, gap_class(reason), reason[:400]),
        )

    def evidence_gaps(self, plan_id: UUID) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT classification, reason, created_at
                FROM agent_evidence_gap WHERE plan_id = %s
                """,
                (plan_id,),
            ).fetchall()
            return [
                {**row, "created_at": row["created_at"].isoformat()}
                for row in rows
            ]

    def record_feedback(self, plan_id: UUID, remaining_budget: int) -> dict:
        """One idempotent proposal after a terminal observation, no tool authority."""
        if not 0 <= remaining_budget <= 32:
            raise ValueError("Remaining gate budget must be 0-32")
        with self.connect() as conn:
            binding = conn.execute(
                "SELECT proposal_id FROM agent_plan WHERE id = %s",
                (plan_id,),
            ).fetchone()
            if not binding:
                raise ValueError("Plan not found")
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s FOR SHARE",
                (binding["proposal_id"],),
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM agent_plan WHERE id = %s FOR UPDATE", (plan_id,)
            ).fetchone()
            if not plan or plan["state"] not in ("COMPLETE", "UNKNOWN"):
                raise ValueError("Only terminal plans can receive feedback")
            existing = conn.execute(
                "SELECT * FROM search_feedback WHERE plan_id = %s", (plan_id,)
            ).fetchone()
            if existing:
                if existing["remaining_budget"] != remaining_budget:
                    raise ValueError("Feedback budget changed after first observation")
                return self._public_feedback(existing)

            result = plan["result"] or {"verdict": "UNKNOWN"}
            decision = decide_feedback(plan["plan"], result, remaining_budget)
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                decision = {
                    "action": "ABSTAIN",
                    "reason": "Oracle approval is no longer current",
                    "policy_version": "fixed-inventory-feedback-v1",
                    "budget_remaining": remaining_budget,
                    "next_step_cost": 0,
                    "automatically_authorized": False,
                }
            next_plan_id = None
            if decision["action"] == "REPLAN":
                next_plan_id = uuid4()
                conn.execute(
                    """
                    INSERT INTO agent_plan(id, proposal_id, plan, strategy)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        next_plan_id, plan["proposal_id"], Jsonb(decision["plan"]),
                        decision["plan"]["strategy"],
                    ),
                )
            row = conn.execute(
                """
                INSERT INTO search_feedback(
                    plan_id, oracle_id, evidence_sha256, policy_version,
                    remaining_budget, decision, next_plan_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    plan_id, plan["proposal_id"], result.get("evidence_sha256"),
                    decision["policy_version"], remaining_budget,
                    Jsonb(decision), next_plan_id,
                ),
            ).fetchone()
            return self._public_feedback(row)

    @staticmethod
    def _public_feedback(row: dict) -> dict:
        return {
            "plan_id": str(row["plan_id"]),
            "oracle_id": str(row["oracle_id"]),
            "evidence_sha256": row["evidence_sha256"],
            "policy_version": row["policy_version"],
            "remaining_budget": row["remaining_budget"],
            "decision": row["decision"],
            "next_plan_id": str(row["next_plan_id"]) if row["next_plan_id"] else None,
        }

    def get_feedback(self, plan_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM search_feedback WHERE plan_id = %s", (plan_id,)
            ).fetchone()
            return self._public_feedback(row) if row else None

    def record_outcome(self, plan_id: UUID, witness: dict) -> bool:
        """Persist one receipt, but never certify incomplete or revoked evidence."""
        from clashbolt.witness import validate_contract

        invalid = validate_contract(witness)
        with self.connect() as conn:
            # Rule first, plan second: matches approval/revocation lock order.
            binding = conn.execute(
                "SELECT proposal_id FROM agent_plan WHERE id = %s", (plan_id,)
            ).fetchone()
            if not binding:
                return False
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s FOR SHARE",
                (binding["proposal_id"],),
            ).fetchone()
            plan = conn.execute(
                "SELECT * FROM agent_plan WHERE id = %s FOR UPDATE", (plan_id,)
            ).fetchone()
            if (
                not plan or plan["state"] != "IN_FLIGHT"
                or plan["proposal_id"] != binding["proposal_id"]
            ):
                return False

            if (
                witness.get("variant") != plan["plan"].get("variant")
                or witness.get("schedule") != plan["plan"].get("schedule")
            ):
                invalid = invalid or "Plan and observed fixture do not match"
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                invalid = invalid or "Oracle was revoked before receipt"
            if not witness.get("complete"):
                invalid = invalid or "Target observation incomplete"
            if witness.get("verdict") not in ("PASS", "FAIL"):
                invalid = invalid or "Oracle could not make a definitive determination"

            status = "UNKNOWN" if invalid else "COMPLETE"
            receipt = {
                "verdict": "UNKNOWN" if invalid else witness["verdict"],
                "evidence_sha256": witness.get("evidence_sha256") if not invalid else None,
                "evidence_gap": invalid,
            }
            updated = conn.execute(
                """
                UPDATE agent_plan SET state = %s, result = %s
                WHERE id = %s AND state = 'IN_FLIGHT' RETURNING id
                """,
                (status, Jsonb(receipt), plan_id),
            ).fetchone()
            if updated and invalid:
                self._record_gap(conn, plan_id, invalid)
            return updated is not None

    def reconcile_stalled_plans(self) -> int:
        """Do not retry uncertain effects; make expired obligations inspectable."""
        with self.connect() as conn:
            expired = conn.execute(
                """
                SELECT id FROM agent_plan
                WHERE state = 'IN_FLIGHT' AND deadline_at < now()
                ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for item in expired:
                conn.execute(
                    """
                    UPDATE agent_plan SET state = 'UNKNOWN',
                        result = %s WHERE id = %s AND state = 'IN_FLIGHT'
                    """,
                    (
                        Jsonb({
                            "verdict": "UNKNOWN",
                            "evidence_sha256": None,
                            "evidence_gap": "Tool receipt missing after deadline",
                        }),
                        item["id"],
                    ),
                )
                self._record_gap(
                    conn, item["id"], "Tool receipt missing after deadline"
                )
            return len(expired)

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
