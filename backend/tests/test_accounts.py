import os
from concurrent.futures import ThreadPoolExecutor

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
