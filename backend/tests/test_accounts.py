import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import psycopg
import pytest
from clashbolt.accounts import (
    LOGIN_ATTEMPTS_PER_MINUTE,
    AccountStore,
    sign_session,
    verify_session,
)


@pytest.fixture
def accounts():
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable PostgreSQL test database is not configured")
    store = AccountStore(dsn)
    store.migrate()
    with store.connect() as conn:
        conn.execute("TRUNCATE account_login_throttle, local_account CASCADE")
    return store


def test_named_accounts_use_password_hashes_and_expiring_sessions(accounts):
    account_id = accounts.create_account(
        "alice.operator", "operator", "correct-horse-battery-test"
    )
    with accounts.connect() as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM local_account WHERE id = %s",
            (account_id,),
        ).fetchone()
    assert b"correct-horse-battery-test" not in row["password_hash"]
    assert len(row["salt"]) == 16
    assert accounts.authenticate("alice.operator", "wrong-long-password") is None
    identity = accounts.authenticate("alice.operator", "correct-horse-battery-test")
    assert identity and identity["role"] == "operator"
    signed = sign_session(identity, "different-session-signing-key-123456789")
    assert verify_session(
        signed, "different-session-signing-key-123456789", accounts
    )["username"] == "alice.operator"
    assert verify_session(
        signed + "bad", "different-session-signing-key-123456789", accounts
    ) is None
    assert accounts.disable_account("alice.operator")
    assert accounts.authenticate("alice.operator", "correct-horse-battery-test") is None
    assert verify_session(signed, "different-session-signing-key-123456789", accounts) is None


def test_local_identity_lockout_and_password_reset(accounts):
    accounts.create_account("bob.reviewer", "reviewer", "original-password-safe-123")
    for _ in range(5):
        assert accounts.authenticate("bob.reviewer", "incorrect-passphrase") is None
    assert accounts.authenticate("bob.reviewer", "original-password-safe-123") is None
    assert accounts.reset_password("bob.reviewer", "brand-new-password-safe-456")
    identity = accounts.authenticate("bob.reviewer", "brand-new-password-safe-456")
    assert identity and identity["role"] == "reviewer"
    with pytest.raises(ValueError, match="already exists"):
        accounts.create_account("bob.reviewer", "viewer", "separate-password-000")
    with pytest.raises(ValueError, match="Password"):
        accounts.create_account("claire", "viewer", "too-short")
    with pytest.raises(ValueError, match="username or role"):
        accounts.create_account("claire", "superadmin", "long-enough-password-000")



def test_account_migration_fails_atomically_on_invalid_new_schema(accounts, monkeypatch):
    import clashbolt.accounts as module

    original = module._ACCOUNT_SCHEMA
    with accounts.connect() as conn:
        before = {
            row["component"]: row["version"] for row in conn.execute(
                "SELECT component, version FROM schema_revision"
            ).fetchall()
        }
    monkeypatch.setattr(module, "_ACCOUNT_SCHEMA", (
        *original,
        "CREATE TABLE this_is_invalid (",
    ))
    with pytest.raises(Exception):
        accounts.migrate()
    with accounts.connect() as conn:
        after = {
            row["component"]: row["version"] for row in conn.execute(
                "SELECT component, version FROM schema_revision"
            ).fetchall()
        }
    assert after == before


def test_login_budget_is_atomic_under_concurrent_requests(accounts):
    """Parallel attempts consume a fixed KDF budget, never exceeding it."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(
            pool.map(
                lambda _: accounts.reserve_login_attempt("127.0.0.1"),
                range(LOGIN_ATTEMPTS_PER_MINUTE + 15),
            )
        )
    assert sum(allowed) == LOGIN_ATTEMPTS_PER_MINUTE
    assert accounts.reserve_login_attempt("127.0.0.1") is False
    with accounts.connect() as conn:
        recorded = conn.execute(
            "SELECT attempts FROM account_login_throttle WHERE peer = %s",
            ("127.0.0.1",),
        ).fetchone()["attempts"]
    assert recorded == LOGIN_ATTEMPTS_PER_MINUTE + 1


def test_login_budget_renews_after_fixed_window(accounts):
    for _ in range(LOGIN_ATTEMPTS_PER_MINUTE):
        assert accounts.reserve_login_attempt("::1")
    assert not accounts.reserve_login_attempt("::1")
    with accounts.connect() as conn:
        conn.execute(
            "UPDATE account_login_throttle "
            "SET window_started = now() - interval '61 seconds' "
            "WHERE peer = %s",
            ("::1",),
        )
    assert accounts.reserve_login_attempt("::1")
    with accounts.connect() as conn:
        count = conn.execute(
            "SELECT attempts FROM account_login_throttle WHERE peer = %s",
            ("::1",),
        ).fetchone()["attempts"]
    assert count == 1


def test_login_budget_rejects_untrusted_peer(accounts):
    assert not accounts.reserve_login_attempt("198.51.100.5")


def test_local_project_role_grants_never_elevate_global_authority(accounts):
    operator = UUID(accounts.create_account(
        "tenant.operator", "operator", "strong-operator-password-123"
    ))
    reviewer = UUID(accounts.create_account(
        "tenant.reviewer", "reviewer", "strong-reviewer-password-123"
    ))
    assert accounts.project_role(operator) == "operator"
    assert accounts.project_role(reviewer) == "reviewer"
    with pytest.raises(ValueError, match="cannot elevate"):
        accounts.grant_local_role("tenant.operator", "reviewer")
    with pytest.raises(ValueError, match="cannot elevate"):
        accounts.grant_local_role("tenant.reviewer", "operator")
    assert accounts.grant_local_role("tenant.operator", "viewer")
    assert accounts.project_role(operator) == "viewer"
    assert accounts.project_role(reviewer) == "reviewer"
    assert accounts.grant_local_role("tenant.operator", "viewer")
    assert accounts.revoke_local_role("tenant.operator")
    assert not accounts.revoke_local_role("tenant.operator")
    assert accounts.project_role(operator) is None
    assert accounts.grant_local_role("tenant.operator", "operator")
    assert accounts.project_role(operator) == "operator"
    with accounts.connect() as conn:
        audit = conn.execute(
            "SELECT action, previous_role, new_role FROM project_role_audit "
            "WHERE account_id = %s ORDER BY id", (operator,),
        ).fetchall()
    assert [(row["action"], row["previous_role"], row["new_role"])
            for row in audit] == [
        ("GRANTED", None, "operator"),
        ("CHANGED", "operator", "viewer"),
        ("REVOKED", "viewer", None),
        ("GRANTED", None, "operator"),
    ]


def test_account_v3_backfill_occurs_once_and_does_not_restore_revoked_grants(accounts):
    account_id = UUID(accounts.create_account(
        "legacy.operator", "operator", "legacy-password-that-is-long"
    ))
    with accounts.connect() as conn:
        conn.execute(
            "DELETE FROM project_membership WHERE account_id = %s",
            (account_id,),
        )
        conn.execute("UPDATE schema_revision SET version = 3 WHERE component = 'accounts'")
    # Simulate one migration of pre-project membership accounts.
    accounts.migrate()
    assert accounts.project_role(account_id) == "operator"
    assert accounts.revoke_local_role("legacy.operator")
    # Idempotent re-migrate must not recreate revoked access.
    accounts.migrate()
    assert accounts.project_role(account_id) is None


def test_project_role_audit_is_database_append_only(accounts):
    account_id = UUID(accounts.create_account(
        "audit.viewer", "viewer", "really-long-password-123456"
    ))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with accounts.connect() as conn:
            conn.execute(
                "DELETE FROM project_role_audit WHERE account_id = %s",
                (account_id,),
            )
    with accounts.connect() as conn:
        audit = conn.execute(
            "SELECT action FROM project_role_audit WHERE account_id = %s",
            (account_id,),
        ).fetchall()
    assert [row["action"] for row in audit] == ["GRANTED"]


def test_project_grant_requires_active_account(accounts):
    accounts.create_account("disabled.operator", "operator", "long-password-123456789")
    assert accounts.disable_account("disabled.operator")
    assert not accounts.grant_local_role("disabled.operator", "operator")
    assert not accounts.grant_local_role("unknown.operator", "operator")
    with pytest.raises(ValueError, match="username"):
        accounts.revoke_local_role("../invalid")


def test_account_cli_grant_does_not_rerun_schema_migration(monkeypatch):
    """Live grant changes must not run DDL alongside API and worker traffic."""
    import sys
    import clashbolt.accounts as module

    calls = []

    class FakeStore:
        def __init__(self, dsn):
            assert dsn == "postgresql://test-only"

        def migrate(self):
            calls.append("migrate")

        def grant_project_role(self, username, role, project):
            calls.append((username, role, project))
            return True

    monkeypatch.setattr(module, "AccountStore", FakeStore)
    monkeypatch.setenv("CLASHBOLT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setattr(sys, "argv", [
        "clashbolt.accounts", "grant-local", "browser.rolesync", "operator",
    ])
    module.main()
    assert calls == [("browser.rolesync", "operator", "local")]

    monkeypatch.setattr(sys, "argv", ["clashbolt.accounts", "migrate"])
    module.main()
    assert calls[-1] == "migrate"
