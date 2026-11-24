"""Operator-approved demo oracles and one-use scoped tool capabilities.

Agent output can propose a plan but cannot approve the oracle or issue authority.
All tool access is local and limited to the shipped synthetic inventory fixture.
"""

import hashlib
import secrets
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.agent_feedback import decide_feedback, gap_class
from clashbolt.agent_history import append_history, verify_history
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
    CREATE TABLE IF NOT EXISTS oracle_audit (
        id BIGSERIAL PRIMARY KEY,
        proposal_id UUID NOT NULL REFERENCES oracle_proposal(id),
        action TEXT NOT NULL CHECK (action IN ('APPROVED', 'REVOKED')),
        actor TEXT NOT NULL,
        rule_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_oracle_audit()
    RETURNS TRIGGER LANGUAGE plpgsql AS $audit_guard$
    BEGIN
        RAISE EXCEPTION 'Oracle approval audit is append-only';
    END
    $audit_guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_oracle_audit_immutable ON oracle_audit",
    """
    CREATE TRIGGER clashbolt_oracle_audit_immutable
    BEFORE UPDATE OR DELETE ON oracle_audit FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_oracle_audit()
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
    "ALTER TABLE tool_capability ADD COLUMN IF NOT EXISTS issuer_account_id UUID",
    "ALTER TABLE tool_capability ADD COLUMN IF NOT EXISTS issuer_project_id TEXT",
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
    """
    CREATE TABLE IF NOT EXISTS agent_history (
        plan_id UUID NOT NULL REFERENCES agent_plan(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 10000),
        event TEXT NOT NULL CHECK (
            event IN (
                'PROPOSED', 'DISPATCHED', 'RECEIPT',
                'TIMEOUT', 'ORACLE_REVOKED', 'FEEDBACK'
            )
        ),
        payload JSONB NOT NULL,
        previous_sha256 TEXT NOT NULL,
        entry_sha256 TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, sequence)
    )
    """,
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_agent_history()
    RETURNS TRIGGER LANGUAGE plpgsql AS $agent_guard$
    BEGIN
        RAISE EXCEPTION 'Agent decision history is append-only';
    END
    $agent_guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_agent_history_immutable ON agent_history",
    """
    CREATE TRIGGER clashbolt_agent_history_immutable
    BEFORE UPDATE OR DELETE ON agent_history FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_agent_history()
    """,
    "ALTER TABLE agent_plan ADD COLUMN IF NOT EXISTS integrity_checked_at TIMESTAMPTZ",
    """
    CREATE INDEX IF NOT EXISTS agent_plan_integrity_sweep
    ON agent_plan (integrity_checked_at, created_at, id)
    WHERE state IN ('PROPOSED', 'IN_FLIGHT')
    """,
    "ALTER TABLE agent_plan ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
    "ALTER TABLE agent_plan ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS agent_obligation (
        plan_id UUID PRIMARY KEY REFERENCES agent_plan(id) ON DELETE CASCADE,
        question_id UUID NOT NULL UNIQUE,
        context_version INTEGER NOT NULL CHECK (context_version = 1),
        tool_schema_version TEXT NOT NULL
            CHECK (tool_schema_version = 'synthetic-tool-v1'),
        model_identifier TEXT NOT NULL
            CHECK (length(model_identifier) BETWEEN 1 AND 80),
        state TEXT NOT NULL CHECK (
            state IN ('PENDING', 'IN_FLIGHT', 'FULFILLED', 'UNKNOWN')
        ),
        tool_request_id UUID UNIQUE,
        receipt_id UUID UNIQUE,
        CHECK (
            state != 'PENDING'
            OR (tool_request_id IS NULL AND receipt_id IS NULL)
        ),
        CHECK (
            state != 'IN_FLIGHT'
            OR (tool_request_id IS NOT NULL AND receipt_id IS NULL)
        ),
        CHECK (receipt_id IS NULL OR tool_request_id IS NOT NULL)
    )
    """,
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
    "ALTER TABLE model_reservation ADD COLUMN IF NOT EXISTS "
    "deadline_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 seconds')",
    "ALTER TABLE model_reservation ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ",
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


CONTEXT_VERSION = 1
TOOL_SCHEMA_VERSION = "synthetic-tool-v1"
POLICY_SCHEMA_VERSION = 11


class Policy(Ledger):
    @classmethod
    def _apply_policy_schema(cls, conn) -> None:
        cls._apply_base_schema(conn)
        for statement in _POLICY_SCHEMA:
            conn.execute(statement)
        _record_schema_revision(conn, "policy", POLICY_SCHEMA_VERSION)

    def migrate(self) -> None:
        """Control and policy schema changes commit atomically as one version."""
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            self._apply_policy_schema(conn)

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
                    state = CASE
                        WHEN %s::integer IS NOT NULL AND deadline_at > now()
                        THEN 'COMPLETE' ELSE 'UNKNOWN'
                    END,
                    finalized_at = now()
                WHERE id = %s AND state = 'RESERVED'
                RETURNING project_id, window_date
                """,
                (observed_tokens, observed_tokens, reservation_id),
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

    def model_reservation_state(self, reservation_id: UUID) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state FROM model_reservation WHERE id = %s",
                (reservation_id,),
            ).fetchone()
            return row["state"] if row else None

    def reconcile_stalled_model_calls(self) -> int:
        """Classify expired model obligations UNKNOWN without refunding budget."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                WITH expired AS (
                    SELECT id FROM model_reservation
                    WHERE state = 'RESERVED' AND deadline_at < now()
                    ORDER BY created_at, id LIMIT 100 FOR UPDATE SKIP LOCKED
                )
                UPDATE model_reservation AS reservations
                SET state = 'UNKNOWN', finalized_at = now()
                FROM expired
                WHERE reservations.id = expired.id
                RETURNING reservations.id
                """
            ).fetchall()
            return len(rows)

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
            state_counts = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE state = 'RESERVED') AS inflight,
                    count(*) FILTER (WHERE state = 'UNKNOWN') AS unknown
                FROM model_reservation
                WHERE project_id = 'local' AND window_date = CURRENT_DATE
                """
            ).fetchone()
            if not row:
                return {
                    "calls_reserved": 0, "tokens_reserved": 0, "tokens_observed": 0,
                    "call_limit": 20, "token_limit": 5120,
                    "calls_inflight": state_counts["inflight"],
                    "calls_unknown": state_counts["unknown"],
                }
            return {
                **row,
                "window_date": row["window_date"].isoformat(),
                "calls_inflight": state_counts["inflight"],
                "calls_unknown": state_counts["unknown"],
            }

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

    def oracle_audit(self, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid audit page limit")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT proposal_id, action, actor, rule_hash, created_at
                FROM oracle_audit ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    **row,
                    "proposal_id": str(row["proposal_id"]),
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]

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
            conn.execute(
                """
                INSERT INTO oracle_audit(proposal_id, action, actor, rule_hash)
                VALUES (%s, 'APPROVED', %s, %s)
                """,
                (proposal_id, reviewer, approved["rule_hash"]),
            )
            return {**approved, "id": str(approved["id"])}

    def revoke_rule(
        self, proposal_id: UUID, actor: str = "local-reviewer"
    ) -> bool:
        if not actor or len(actor) > 100:
            raise ValueError("Reviewer identity is required")
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s FOR UPDATE",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED":
                return False
            conn.execute(
                "UPDATE oracle_proposal SET state = 'REVOKED' WHERE id = %s",
                (proposal_id,),
            )
            conn.execute(
                """
                INSERT INTO oracle_audit(proposal_id, action, actor, rule_hash)
                VALUES (%s, 'REVOKED', %s, %s)
                """,
                (proposal_id, actor, rule["rule_hash"]),
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
                conn.execute(
                    """
                    UPDATE agent_obligation SET state = 'UNKNOWN'
                    WHERE plan_id = %s
                    """,
                    (plan["id"],),
                )
                self._record_gap(conn, plan["id"], "Oracle approval revoked")
                append_history(conn, plan["id"], "ORACLE_REVOKED", {
                    "historical_evidence_sha256": receipt.get("evidence_sha256"),
                    "new_state": "UNKNOWN",
                })
            return True

    @staticmethod
    def _issuer_has_project_operator_grant(
        conn, account_id: UUID, project_id: str,
    ) -> bool:
        """Lock global identity and grant before any project capability debit.

        A tenant connection has no public search_path fallback: explicitly
        qualify the authoritative identity/grant tables. Role changes lock the
        same account row before revoking tenant capabilities.
        """
        row = conn.execute(
            """
            SELECT a.id FROM public.local_account a
            JOIN public.project_membership m ON m.account_id = a.id
            JOIN public.project_workspace w ON w.project_id = m.project_id
            WHERE a.id = %s AND a.active = TRUE AND a.role = 'operator'
              AND m.project_id = %s AND m.role = 'operator'
            FOR SHARE OF a, m
            """,
            (account_id, project_id),
        ).fetchone()
        return row is not None

    def issue(
        self,
        proposal_id: UUID,
        variant: str,
        schedule: str,
        max_calls: int = 1,
        lifetime_seconds: int = 600,
        plan_id: UUID | None = None,
        issuer_account_id: UUID | None = None,
    ) -> dict:
        if issuer_account_id is not None and not isinstance(issuer_account_id, UUID):
            raise ValueError("Capability issuer must be an authenticated account UUID")
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
                    or self._validated_plan_history(conn, planned)[0]["status"]
                        != "VERIFIED"
                ):
                    raise ValueError("Capability must match one pending approved plan")
            if issuer_account_id is not None and not self._issuer_has_project_operator_grant(
                conn, issuer_account_id, self.project_id
            ):
                raise ValueError("Issuer no longer has local operator authority")
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
            if issuer_account_id is not None:
                # Account schema adds these columns with foreign keys. Legacy
                # policy-only installations never write or depend on them.
                conn.execute(
                    """
                    UPDATE tool_capability
                    SET issuer_account_id = %s, issuer_project_id = %s
                    WHERE id = %s
                    """,
                    (issuer_account_id, self.project_id, cap["id"]),
                )
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
        self, token: str, variant: str, schedule: str, plan_id: UUID | None = None,
        *, accounts_mode: bool = False,
    ) -> bool:
        """Serialize oracle approval, capability debit and plan dispatch.

        Lock oracle, then the issuing account and membership, then the
        capability and plan. Account revocation locks the account before
        revoking capabilities; preserving that order prevents deadlocks.
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as conn:
            candidate = conn.execute(
                "SELECT * FROM tool_capability WHERE token_hash = %s",
                (token_hash,),
            ).fetchone()
            oracle = None
            if candidate:
                oracle = conn.execute(
                    """
                    SELECT state, rule_hash FROM oracle_proposal
                    WHERE id = %s FOR SHARE
                    """,
                    (candidate["proposal_id"],),
                ).fetchone()
            # Lock issuer identity *before* locking the capability row.
            # All account mutations take the account row FOR UPDATE before
            # revoking tokens. This closes a cap/account lock inversion.
            issuer = candidate.get("issuer_account_id") if candidate else None
            if accounts_mode:
                issuer_ok = bool(
                    issuer is not None
                    and candidate.get("issuer_project_id") == self.project_id
                    and self._issuer_has_project_operator_grant(
                        conn, issuer, self.project_id
                    )
                )
            else:
                issuer_ok = issuer is None
            row = conn.execute(
                "SELECT * FROM tool_capability WHERE token_hash = %s FOR UPDATE",
                (token_hash,),
            ).fetchone() if candidate else None
            # A privileged actor must not be able to swap capability
            # ownership between the initial read and the locked re-read.
            issuer_ok = bool(
                issuer_ok and row
                and row.get("issuer_account_id") == issuer
                and row.get("issuer_project_id")
                    == candidate.get("issuer_project_id")
            )
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
                    and self._validated_plan_history(conn, planned)[0]["status"]
                        == "VERIFIED"
                )
            elif row and row["plan_id"] is not None:
                plan_ok = False
            # The current issuer/role is verified while its database row
            # locks are held; even a copied token cannot outlive revocation.
            allowed = bool(
                row
                and issuer_ok
                and plan_ok
                and oracle is not None
                and oracle["state"] == "APPROVED"
                and oracle["rule_hash"] == ORACLE_HASH
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
                    tool_request_id = uuid4()
                    obligation = conn.execute(
                        """
                        UPDATE agent_obligation
                        SET state = 'IN_FLIGHT', tool_request_id = %s
                        WHERE plan_id = %s AND state = 'PENDING'
                        RETURNING question_id
                        """,
                        (tool_request_id, plan_id),
                    ).fetchone()
                    if not obligation:
                        raise ValueError("Agent context changed during tool authorization")
                    conn.execute(
                        """
                        UPDATE agent_plan SET state = 'IN_FLIGHT',
                            started_at = now(),
                            deadline_at = now() + interval '30 seconds'
                        WHERE id = %s AND state = 'PROPOSED'
                        """,
                        (plan_id,),
                    )
                    append_history(conn, plan_id, "DISPATCHED", {
                        "capability_id": str(row["id"]),
                        "variant": variant,
                        "schedule": schedule,
                        "question_id": str(obligation["question_id"]),
                        "tool_request_id": str(tool_request_id),
                        "tool_schema_version": TOOL_SCHEMA_VERSION,
                    })
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
                  AND (
                      result IS NULL OR result->>'quarantined' IS DISTINCT FROM 'true'
                  )
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

    @staticmethod
    def _new_agent_plan(conn, proposal_id: UUID, plan: dict) -> dict:
        """Write proposal, one outstanding question and history atomically."""
        model_identifier = plan.get("model_id") or plan.get(
            "planner_kind", "DETERMINISTIC_BASELINE"
        )
        if (
            not isinstance(model_identifier, str)
            or not 1 <= len(model_identifier) <= 80
        ):
            raise ValueError("Agent model identifier is not bounded")
        row = conn.execute(
            """
            INSERT INTO agent_plan(id, proposal_id, plan, strategy)
            VALUES (%s, %s, %s, %s)
            RETURNING id, strategy, state
            """,
            (uuid4(), proposal_id, Jsonb(plan), plan["strategy"]),
        ).fetchone()
        question_id = uuid4()
        conn.execute(
            """
            INSERT INTO agent_obligation(
                plan_id, question_id, context_version, tool_schema_version,
                model_identifier, state
            ) VALUES (%s, %s, %s, %s, %s, 'PENDING')
            """,
            (
                row["id"], question_id, CONTEXT_VERSION,
                TOOL_SCHEMA_VERSION, model_identifier,
            ),
        )
        context = {
            "question_id": str(question_id),
            "context_version": CONTEXT_VERSION,
            "tool_schema_version": TOOL_SCHEMA_VERSION,
            "model_identifier": model_identifier,
        }
        append_history(conn, row["id"], "PROPOSED", {
            "proposal_id": str(proposal_id),
            "rule_sha256": ORACLE_HASH,
            "plan": plan,
            "obligation": context,
        })
        return {
            **row, "id": str(row["id"]), "plan": plan,
            "obligation": {
                **context, "state": "PENDING",
                "tool_request_id": None, "receipt_id": None,
            },
        }

    @staticmethod
    def _public_obligation(row: dict | None) -> dict | None:
        if row is None:
            return None
        return {
            "question_id": str(row["question_id"]),
            "context_version": row["context_version"],
            "tool_schema_version": row["tool_schema_version"],
            "model_identifier": row["model_identifier"],
            "state": row["state"],
            "tool_request_id": (
                str(row["tool_request_id"]) if row["tool_request_id"] else None
            ),
            "receipt_id": str(row["receipt_id"]) if row["receipt_id"] else None,
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
                  AND (
                      result IS NULL OR result->>'quarantined' IS DISTINCT FROM 'true'
                  )
                ORDER BY created_at, id LIMIT 1000
                """,
                (proposal_id,),
            ).fetchall()
            plan = next_frontier_plan(evidence, variant)
            return self._new_agent_plan(conn, proposal_id, plan)

    def record_plan(self, proposal_id: UUID, plan: dict) -> dict:
        """Persist typed deterministic planning intent; no tool call is implied."""
        with self.connect() as conn:
            rule = conn.execute(
                "SELECT state, rule_hash FROM oracle_proposal WHERE id = %s",
                (proposal_id,),
            ).fetchone()
            if not rule or rule["state"] != "APPROVED" or rule["rule_hash"] != ORACLE_HASH:
                raise ValueError("Only approved oracle versions may be planned")
            return self._new_agent_plan(conn, proposal_id, plan)

    @staticmethod
    def _validated_plan_history(conn, planned: dict) -> tuple[dict, list[dict]]:
        """Recheck the immutable proposal against the mutable plan before use."""
        rows = conn.execute(
            """
            SELECT sequence, event, payload, previous_sha256, entry_sha256,
                created_at
            FROM agent_history WHERE plan_id = %s ORDER BY sequence
            """,
            (planned["id"],),
        ).fetchall()
        integrity = verify_history(planned["id"], rows)
        if integrity["status"] == "VERIFIED":
            initial = rows[0]["payload"]
            obligation = conn.execute(
                "SELECT * FROM agent_obligation WHERE plan_id = %s",
                (planned["id"],),
            ).fetchone()
            context = (
                {
                    "question_id": str(obligation["question_id"]),
                    "context_version": obligation["context_version"],
                    "tool_schema_version": obligation["tool_schema_version"],
                    "model_identifier": obligation["model_identifier"],
                }
                if obligation else None
            )
            dispatched = [
                item["payload"] for item in rows if item["event"] == "DISPATCHED"
            ]
            closure = [
                item for item in rows
                if item["event"] in ("RECEIPT", "TIMEOUT", "ORACLE_REVOKED")
            ]
            if (
                initial.get("plan") != planned["plan"]
                or initial.get("proposal_id") != str(planned["proposal_id"])
                or initial.get("rule_sha256") != ORACLE_HASH
                or context is None
                or initial.get("obligation") != context
                or context["context_version"] != CONTEXT_VERSION
                or context["tool_schema_version"] != TOOL_SCHEMA_VERSION
            ):
                integrity = {
                    "status": "INVALID",
                    "reason": "Stored agent plan or context differs from approved decision",
                }
            elif (
                (planned["state"] == "PROPOSED"
                 and (
                     obligation["state"] != "PENDING"
                     or obligation["tool_request_id"] is not None
                     or obligation["receipt_id"] is not None
                     or dispatched or closure
                 ))
                or (planned["state"] == "IN_FLIGHT"
                    and (
                        obligation["state"] != "IN_FLIGHT"
                        or obligation["tool_request_id"] is None
                        or obligation["receipt_id"] is not None
                        or len(dispatched) != 1 or closure
                    ))
                or (planned["state"] == "COMPLETE"
                    and (
                        obligation["state"] != "FULFILLED"
                        or obligation["receipt_id"] is None
                        or not closure or closure[-1]["event"] != "RECEIPT"
                    ))
                or (planned["state"] == "UNKNOWN"
                    and (
                        obligation["state"] != "UNKNOWN"
                        or not closure
                    ))
            ):
                integrity = {
                    "status": "INVALID",
                    "reason": "Agent obligation and decision state disagree",
                }
            elif dispatched and (
                dispatched[0].get("question_id") != context["question_id"]
                or dispatched[0].get("tool_request_id")
                    != str(obligation["tool_request_id"])
                or dispatched[0].get("tool_schema_version") != TOOL_SCHEMA_VERSION
            ):
                integrity = {
                    "status": "INVALID",
                    "reason": "Agent tool request identity differs from recorded intent",
                }
            elif closure and closure[-1]["event"] == "RECEIPT" and (
                closure[-1]["payload"].get("question_id") != context["question_id"]
                or closure[-1]["payload"].get("tool_request_id")
                    != str(obligation["tool_request_id"])
                or closure[-1]["payload"].get("receipt_id")
                    != str(obligation["receipt_id"])
            ):
                integrity = {
                    "status": "INVALID",
                    "reason": "Agent receipt identity differs from recorded obligation",
                }
            elif closure and closure[-1]["event"] == "TIMEOUT" and (
                closure[-1]["payload"].get("question_id") != context["question_id"]
                or closure[-1]["payload"].get("tool_request_id")
                    != str(obligation["tool_request_id"])
            ):
                integrity = {
                    "status": "INVALID",
                    "reason": "Timed-out agent request identity is inconsistent",
                }
        return integrity, rows

    def reconcile_corrupt_plans(self, limit: int = 20) -> int:
        """Quarantine persistent agent context drift without any tool retry.

        Inspect a rotating, age-limited set of active plans in deterministic
        row-lock order. A corrupt or missing history becomes terminal UNKNOWN;
        only a separately approved new plan can ever regain execution rights.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Invalid agent integrity sweep batch size")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_plan
                WHERE state IN ('PROPOSED', 'IN_FLIGHT')
                  AND (
                      integrity_checked_at IS NULL
                      OR integrity_checked_at < now() - interval '30 seconds'
                  )
                ORDER BY integrity_checked_at NULLS FIRST, created_at, id
                LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            ).fetchall()
            quarantined = 0
            for planned in rows:
                integrity, _ = self._validated_plan_history(conn, planned)
                if integrity["status"] == "VERIFIED":
                    conn.execute(
                        """
                        UPDATE agent_plan SET integrity_checked_at = now()
                        WHERE id = %s
                        """,
                        (planned["id"],),
                    )
                    continue
                reason = (
                    "Agent decision history is "
                    f"{integrity['status']}: {integrity['reason']}"
                )
                conn.execute(
                    """
                    UPDATE agent_plan
                    SET state = 'UNKNOWN', integrity_checked_at = now(),
                        result = %s
                    WHERE id = %s
                    """,
                    (Jsonb({
                        "verdict": "UNKNOWN",
                        "evidence_sha256": None,
                        "evidence_gap": reason,
                        "quarantined": True,
                    }), planned["id"]),
                )
                conn.execute(
                    """
                    UPDATE agent_obligation SET state = 'UNKNOWN'
                    WHERE plan_id = %s
                    """,
                    (planned["id"],),
                )
                self._record_gap(conn, planned["id"], reason)
                quarantined += 1
            return quarantined

    def plan_history(self, plan_id: UUID) -> dict | None:
        with self.connect() as conn:
            plan = conn.execute(
                "SELECT id, proposal_id, plan, state FROM agent_plan WHERE id = %s",
                (plan_id,),
            ).fetchone()
            if not plan:
                return None
            status, entries = self._validated_plan_history(conn, plan)
            obligation = conn.execute(
                "SELECT * FROM agent_obligation WHERE plan_id = %s", (plan_id,)
            ).fetchone()
            return {
                "plan_id": str(plan_id),
                "current_state": plan["state"],
                "obligation": self._public_obligation(obligation),
                "integrity": status,
                "entries": [
                    {**row, "created_at": row["created_at"].isoformat()}
                    for row in entries
                ],
            }

    def get_plan(self, plan_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, proposal_id, plan, state, result FROM agent_plan WHERE id = %s",
                (plan_id,),
            ).fetchone()
            if not row:
                return None
            obligation = conn.execute(
                "SELECT * FROM agent_obligation WHERE plan_id = %s", (plan_id,)
            ).fetchone()
            return {
                **row,
                "id": str(row["id"]),
                "proposal_id": str(row["proposal_id"]),
                "obligation": self._public_obligation(obligation),
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
            if plan["result"] and plan["result"].get("quarantined"):
                raise ValueError("Quarantined decision context cannot be replanned")
            if self._validated_plan_history(conn, plan)[0]["status"] != "VERIFIED":
                raise ValueError("Corrupted agent decision history cannot be replanned")
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
                created = self._new_agent_plan(
                    conn, plan["proposal_id"], decision["plan"]
                )
                next_plan_id = UUID(created["id"])
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
            append_history(conn, plan_id, "FEEDBACK", {
                "decision": decision,
                "next_plan_id": str(next_plan_id) if next_plan_id else None,
            })
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
        if not isinstance(witness, dict):
            witness = {}
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

            if self._validated_plan_history(conn, plan)[0]["status"] != "VERIFIED":
                invalid = invalid or "Agent plan history is missing or corrupted"
            if (
                plan["deadline_at"] is None
                or plan["deadline_at"]
                    <= conn.execute("SELECT now() AS now").fetchone()["now"]
            ):
                invalid = invalid or "Tool receipt arrived after deadline"
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
            if updated:
                receipt_id = uuid4()
                obligation = conn.execute(
                    """
                    UPDATE agent_obligation
                    SET state = %s,
                        receipt_id = CASE
                            WHEN tool_request_id IS NOT NULL THEN %s
                            ELSE NULL END
                    WHERE plan_id = %s
                    RETURNING question_id, tool_request_id, receipt_id
                    """,
                    (
                        "UNKNOWN" if invalid else "FULFILLED",
                        receipt_id, plan_id,
                    ),
                ).fetchone()
                if invalid:
                    self._record_gap(conn, plan_id, invalid)
                append_history(conn, plan_id, "RECEIPT", {
                    "state": status,
                    "receipt": receipt,
                    "question_id": (
                        str(obligation["question_id"]) if obligation else None
                    ),
                    "tool_request_id": (
                        str(obligation["tool_request_id"])
                        if obligation and obligation["tool_request_id"] else None
                    ),
                    "receipt_id": (
                        str(obligation["receipt_id"])
                        if obligation and obligation["receipt_id"] else None
                    ),
                })
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
                obligation = conn.execute(
                    """
                    UPDATE agent_obligation SET state = 'UNKNOWN'
                    WHERE plan_id = %s
                    RETURNING question_id, tool_request_id
                    """,
                    (item["id"],),
                ).fetchone()
                self._record_gap(
                    conn, item["id"], "Tool receipt missing after deadline"
                )
                append_history(conn, item["id"], "TIMEOUT", {
                    "state": "UNKNOWN",
                    "reason": "Tool receipt missing after deadline",
                    "question_id": (
                        str(obligation["question_id"]) if obligation else None
                    ),
                    "tool_request_id": (
                        str(obligation["tool_request_id"])
                        if obligation and obligation["tool_request_id"] else None
                    ),
                })
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
