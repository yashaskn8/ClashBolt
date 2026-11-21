"""Named local accounts with an explicit, fail-closed local project grant.

Accounts are provisioned from the trusted local CLI, not HTTP signup.
Only the shipped local project has scoped resources; there is no general
multi-project API, SSO, or Internet-facing authentication.
"""

import argparse
import getpass
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from clashbolt.policy import Policy
from clashbolt.store import SCHEMA_LOCK_ID, _record_schema_revision

ROLES = ("viewer", "operator", "reviewer")
USERNAME = re.compile(r"[a-z][a-z0-9._-]{2,47}\Z")
KDF_ITERATIONS = 450_000
ACCOUNT_SCHEMA_VERSION = 4
LOGIN_ATTEMPTS_PER_MINUTE = 20

_ACCOUNT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS local_account (
        id UUID PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'reviewer')),
        salt BYTEA NOT NULL,
        password_hash BYTEA NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        session_epoch BIGINT NOT NULL DEFAULT 1,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_experiment_draft (
        account_id UUID PRIMARY KEY REFERENCES local_account(id) ON DELETE CASCADE,
        version BIGINT NOT NULL DEFAULT 1,
        draft JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_login_throttle (
        peer TEXT PRIMARY KEY,
        window_started TIMESTAMPTZ NOT NULL,
        attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 21)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_membership (
        project_id TEXT NOT NULL REFERENCES project_quota(project_id),
        account_id UUID NOT NULL REFERENCES local_account(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'reviewer')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_role_audit (
        id BIGSERIAL PRIMARY KEY,
        project_id TEXT NOT NULL,
        account_id UUID NOT NULL REFERENCES local_account(id),
        action TEXT NOT NULL CHECK (action IN ('GRANTED', 'CHANGED', 'REVOKED')),
        previous_role TEXT,
        new_role TEXT,
        actor TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE OR REPLACE FUNCTION clashbolt_guard_project_role_audit()
    RETURNS TRIGGER LANGUAGE plpgsql AS $role_guard$
    BEGIN
        RAISE EXCEPTION 'Project role audit is append-only';
    END
    $role_guard$
    """,
    "DROP TRIGGER IF EXISTS clashbolt_project_role_audit_immutable ON project_role_audit",
    """
    CREATE TRIGGER clashbolt_project_role_audit_immutable
    BEFORE UPDATE OR DELETE ON project_role_audit FOR EACH ROW
    EXECUTE FUNCTION clashbolt_guard_project_role_audit()
    """,
)


def _digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, KDF_ITERATIONS, dklen=32
    )


def _valid_password(password: str) -> bool:
    return 16 <= len(password) <= 256 and len(password.encode("utf-8")) <= 512


class AccountStore(Policy):
    def migrate(self) -> None:
        """Apply control, approval and account revisions in one transaction."""
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_ID,))
            self._apply_policy_schema(conn)
            previous = conn.execute(
                "SELECT version FROM schema_revision WHERE component = 'accounts'"
            ).fetchone()
            for statement in _ACCOUNT_SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO project_quota(project_id) VALUES ('local') ON CONFLICT DO NOTHING"
            )
            if previous is None or previous["version"] < 4:
                # One-time migration of existing named accounts. Never repeat
                # this backfill or it would silently restore revoked grants.
                conn.execute(
                    """
                    INSERT INTO project_membership(project_id, account_id, role)
                    SELECT 'local', id, role FROM local_account
                    ON CONFLICT DO NOTHING
                    """
                )
            _record_schema_revision(conn, "accounts", ACCOUNT_SCHEMA_VERSION)

    def reserve_login_attempt(self, peer: str) -> bool:
        """Atomically bound password KDF work per direct local peer.

        Do not use X-Forwarded-For here: only the trusted socket peer is
        available until a separately audited reverse-proxy boundary exists.
        The fixed one-minute window does not lock accounts or reset sessions.
        """
        if peer not in ("127.0.0.1", "::1", "testclient"):
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO account_login_throttle(peer, window_started, attempts)
                VALUES (%s, now(), 1)
                ON CONFLICT (peer) DO UPDATE
                SET attempts = CASE
                        WHEN account_login_throttle.window_started <= now()
                             - interval '1 minute'
                        THEN 1
                        ELSE LEAST(account_login_throttle.attempts + 1, 21)
                    END,
                    window_started = CASE
                        WHEN account_login_throttle.window_started <= now()
                             - interval '1 minute'
                        THEN now()
                        ELSE account_login_throttle.window_started
                    END
                RETURNING attempts
                """,
                (peer,),
            ).fetchone()
            return row["attempts"] <= LOGIN_ATTEMPTS_PER_MINUTE

    def project_role(self, account_id: UUID, project_id: str = "local") -> str | None:
        """Resolve effective project authority from the database every request.

        The legacy account role is an upper bound. An operator cannot be
        promoted to reviewer (or vice versa) by a membership row.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT m.role AS project_role, a.role AS account_role
                FROM project_membership m JOIN local_account a ON a.id = m.account_id
                WHERE m.account_id = %s AND m.project_id = %s AND a.active = TRUE
                """,
                (account_id, project_id),
            ).fetchone()
            if not row:
                return None
            return (
                row["project_role"]
                if row["project_role"] in ("viewer", row["account_role"])
                else None
            )

    def grant_local_role(self, username: str, role: str) -> bool:
        """Trusted CLI-only grant; cannot expand the account's role ceiling."""
        if not USERNAME.fullmatch(username) or role not in ROLES:
            raise ValueError("Invalid local username or project role")
        with self.connect() as conn:
            account = conn.execute(
                """
                SELECT id, role FROM local_account
                WHERE username = %s AND active = TRUE FOR UPDATE
                """,
                (username,),
            ).fetchone()
            if not account:
                return False
            if role not in ("viewer", account["role"]):
                raise ValueError("A project grant cannot elevate the account's role")
            previous = conn.execute(
                """
                SELECT role FROM project_membership
                WHERE project_id = 'local' AND account_id = %s FOR UPDATE
                """,
                (account["id"],),
            ).fetchone()
            if previous and previous["role"] == role:
                return True
            conn.execute(
                """
                INSERT INTO project_membership(project_id, account_id, role)
                VALUES ('local', %s, %s)
                ON CONFLICT (project_id, account_id)
                DO UPDATE SET role = EXCLUDED.role
                """,
                (account["id"], role),
            )
            conn.execute(
                """
                INSERT INTO project_role_audit(
                    project_id, account_id, action, previous_role, new_role, actor
                ) VALUES ('local', %s, %s, %s, %s, 'trusted-local-cli')
                """,
                (
                    account["id"], "CHANGED" if previous else "GRANTED",
                    previous["role"] if previous else None, role,
                ),
            )
            return True

    def revoke_local_role(self, username: str) -> bool:
        """Removal immediately denies all local resource requests, even with a cookie."""
        if not USERNAME.fullmatch(username):
            raise ValueError("Invalid local username")
        with self.connect() as conn:
            account = conn.execute(
                "SELECT id FROM local_account WHERE username = %s FOR UPDATE",
                (username,),
            ).fetchone()
            if not account:
                return False
            previous = conn.execute(
                """
                DELETE FROM project_membership
                WHERE project_id = 'local' AND account_id = %s RETURNING role
                """,
                (account["id"],),
            ).fetchone()
            if not previous:
                return False
            conn.execute(
                """
                INSERT INTO project_role_audit(
                    project_id, account_id, action, previous_role, new_role, actor
                ) VALUES ('local', %s, 'REVOKED', %s, NULL, 'trusted-local-cli')
                """,
                (account["id"], previous["role"]),
            )
            return True

    def get_account_draft(self, account_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT version, draft, updated_at
                FROM account_experiment_draft WHERE account_id = %s
                """,
                (account_id,),
            ).fetchone()
            return {**row, "updated_at": row["updated_at"].isoformat()} if row else None

    def save_account_draft(
        self, account_id: UUID, draft: dict, expected_version: int
    ) -> dict:
        """One optimistic-versioned draft per named account, never shared."""
        self._validate_draft(draft, expected_version)
        with self.connect() as conn:
            if expected_version == 0:
                row = conn.execute(
                    """
                    INSERT INTO account_experiment_draft(account_id, version, draft)
                    VALUES (%s, 1, %s)
                    ON CONFLICT (account_id) DO NOTHING
                    RETURNING version, draft, updated_at
                    """,
                    (account_id, Jsonb(draft)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    UPDATE account_experiment_draft
                    SET version = version + 1, draft = %s, updated_at = now()
                    WHERE account_id = %s AND version = %s
                    RETURNING version, draft, updated_at
                    """,
                    (Jsonb(draft), account_id, expected_version),
                ).fetchone()
            if not row:
                raise ValueError("Stale account experiment draft version")
            return {**row, "updated_at": row["updated_at"].isoformat()}

    def create_account(self, username: str, role: str, password: str) -> str:
        if not USERNAME.fullmatch(username) or role not in ROLES:
            raise ValueError("Invalid local username or role")
        if not _valid_password(password):
            raise ValueError("Password must contain 16-256 characters")
        salt = secrets.token_bytes(16)
        digest = _digest(password, salt)
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO local_account(
                    id, username, role, salt, password_hash
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
                """,
                (uuid4(), username, role, salt, digest),
            ).fetchone()
            if not row:
                raise ValueError("Account already exists")
            conn.execute(
                """
                INSERT INTO project_membership(project_id, account_id, role)
                VALUES ('local', %s, %s)
                """,
                (row["id"], role),
            )
            conn.execute(
                """
                INSERT INTO project_role_audit(
                    project_id, account_id, action, new_role, actor
                ) VALUES ('local', %s, 'GRANTED', %s, 'trusted-local-cli')
                """,
                (row["id"], role),
            )
            return str(row["id"])

    def authenticate(self, username: str, password: str) -> dict | None:
        if (
            not USERNAME.fullmatch(username)
            or len(password) > 256
            or len(password.encode("utf-8")) > 512
        ):
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_account WHERE username = %s FOR UPDATE",
                (username,),
            ).fetchone()
            # A missing username still incurs a password KDF to reduce enumeration.
            expected = row["password_hash"] if row else bytes(32)
            salt = row["salt"] if row else bytes(16)
            matches = hmac.compare_digest(_digest(password, salt), expected)
            if row is None or not row["active"]:
                return None
            now = datetime.now(timezone.utc)
            if row["locked_until"] and row["locked_until"] > now:
                return None
            if not matches:
                conn.execute(
                    """
                    UPDATE local_account
                    SET failed_logins = failed_logins + 1,
                        locked_until = CASE
                            WHEN failed_logins + 1 >= 5
                            THEN now() + interval '5 minutes'
                            ELSE NULL END
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                return None
            conn.execute(
                """
                UPDATE local_account
                SET failed_logins = 0, locked_until = NULL, last_login_at = now()
                WHERE id = %s
                """,
                (row["id"],),
            )
            return {
                "id": str(row["id"]),
                "username": row["username"],
                "role": row["role"],
                "epoch": row["session_epoch"],
            }

    def get_active(self, account_id: UUID, epoch: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, role, session_epoch FROM local_account
                WHERE id = %s AND active = TRUE AND session_epoch = %s
                """,
                (account_id, epoch),
            ).fetchone()
            if not row:
                return None
            return {
                "id": str(row["id"]),
                "username": row["username"],
                "role": row["role"],
                "epoch": row["session_epoch"],
            }

    def revoke_sessions(self, account_id: UUID) -> bool:
        """Logging out revokes all sessions, including any copied cookies."""
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE local_account
                SET session_epoch = session_epoch + 1
                WHERE id = %s AND active = TRUE RETURNING id
                """,
                (account_id,),
            ).fetchone()
            return bool(result)

    def disable_account(self, username: str) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE local_account
                SET active = FALSE, session_epoch = session_epoch + 1
                WHERE username = %s AND active = TRUE RETURNING id
                """,
                (username,),
            ).fetchone()
            return bool(result)

    def reset_password(self, username: str, password: str) -> bool:
        if not _valid_password(password):
            raise ValueError("Password must contain 16-256 characters")
        salt = secrets.token_bytes(16)
        digest = _digest(password, salt)
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE local_account
                SET salt = %s, password_hash = %s, failed_logins = 0,
                    locked_until = NULL, session_epoch = session_epoch + 1
                WHERE username = %s AND active = TRUE RETURNING id
                """,
                (salt, digest, username),
            ).fetchone()
            return bool(result)


def sign_session(identity: dict, secret: str) -> str:
    if len(secret) < 32:
        raise ValueError("The account session key must contain at least 32 characters")
    payload = (
        f"{int(time.time()) + 3600}.{identity['id']}.{identity['epoch']}."
        f"{secrets.token_urlsafe(18)}"
    )
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def verify_session(
    session: str | None, secret: str, store: AccountStore
) -> dict | None:
    if not session or not secret or len(session) > 400:
        return None
    try:
        expiry, identity, epoch, nonce, signature = session.split(".")
        now = int(time.time())
        if (
            not expiry.isdigit()
            or not epoch.isdigit()
            or len(nonce) < 16
            or len(signature) != 64
            or not now < int(expiry) <= now + 3600
        ):
            return None
        payload = ".".join((expiry, identity, epoch, nonce))
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        return store.get_active(UUID(identity), int(epoch))
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage locally provisioned ClashBolt accounts")
    parser.add_argument(
        "command", choices=(
            "migrate", "create", "disable", "reset", "grant-local", "revoke-local"
        )
    )
    parser.add_argument("username", nargs="?")
    parser.add_argument("role", nargs="?", choices=ROLES)
    args = parser.parse_args()
    dsn = os.getenv("CLASHBOLT_DATABASE_URL")
    if not dsn:
        parser.error("Set CLASHBOLT_DATABASE_URL first")
    store = AccountStore(dsn)
    store.migrate()
    if args.command == "migrate":
        print("Local account schema ready")
        return
    if not args.username or not USERNAME.fullmatch(args.username):
        parser.error("Specify a lowercase username (3-48 characters)")
    if args.command == "disable":
        if not store.disable_account(args.username):
            parser.error("Account not found or already disabled")
    elif args.command == "revoke-local":
        if not store.revoke_local_role(args.username):
            parser.error("Local project grant not found")
    elif args.command == "grant-local":
        if not args.role:
            parser.error("Project grant requires viewer, operator or reviewer role")
        try:
            if not store.grant_local_role(args.username, args.role):
                parser.error("Active account not found")
        except ValueError as error:
            parser.error(str(error))
    elif args.command in ("create", "reset"):
        if args.command == "create" and not args.role:
            parser.error("Account creation requires viewer, operator or reviewer role")
        password = getpass.getpass("New local account password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("Passwords do not match")
        try:
            if args.command == "create":
                store.create_account(args.username, args.role, password)
            elif not store.reset_password(args.username, password):
                parser.error("Account not found or disabled")
        except ValueError as error:
            parser.error(str(error))
    print(f"{args.command} completed for {args.username}")


if __name__ == "__main__":
    main()
