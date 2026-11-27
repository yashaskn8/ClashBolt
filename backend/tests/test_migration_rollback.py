"""A failed control-schema upgrade must not leave partial PostgreSQL changes."""

import os
from uuid import uuid4

import pytest
from clashbolt.policy import Policy
from psycopg import sql


def test_interrupted_migration_rolls_back_ddl_and_rows(monkeypatch):
    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable CI PostgreSQL database required")
    store = Policy(dsn)
    store.migrate()
    table_name = "cb_rollback_" + uuid4().hex[:20]
    with store.connect() as conn:
        assert conn.info.dbname == "clashbolt_test"
        assert conn.execute(
            "SELECT to_regclass(%s) AS table_name", (f"public.{table_name}",)
        ).fetchone()["table_name"] is None

    original = Policy._apply_policy_schema.__func__

    def interrupted_upgrade(cls, conn):
        original(cls, conn)
        conn.execute(
            sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(table_name))
        )
        conn.execute(
            "INSERT INTO project_quota(project_id) VALUES (%s)", (table_name,)
        )
        raise RuntimeError("injected failure after migration DDL and DML")

    monkeypatch.setattr(Policy, "_apply_policy_schema", classmethod(interrupted_upgrade))
    with pytest.raises(RuntimeError, match="injected failure"):
        store.migrate()

    with store.connect() as conn:
        assert conn.execute(
            "SELECT to_regclass(%s) AS table_name", (f"public.{table_name}",)
        ).fetchone()["table_name"] is None
        assert conn.execute(
            "SELECT count(*) AS n FROM project_quota WHERE project_id = %s",
            (table_name,),
        ).fetchone()["n"] == 0

    # The existing schema is still usable and can be migrated idempotently.
    monkeypatch.setattr(Policy, "_apply_policy_schema", classmethod(original))
    store.migrate()
    assert store.operations()["runs"] is not None


def test_account_store_multi_schema_migration_rolls_back_atomically(monkeypatch):
    from clashbolt.accounts import ACCOUNT_SCHEMA_VERSION, AccountStore
    from clashbolt.policy import POLICY_SCHEMA_VERSION
    from clashbolt.store import BASE_SCHEMA_VERSION

    dsn = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable CI PostgreSQL database required")
    accounts = AccountStore(dsn)
    accounts.migrate()
    project_id = f"rb-{uuid4().hex[:10]}"
    project_schema = accounts.create_project(project_id)
    table_name = "cb_multi_rb_" + uuid4().hex[:16]

    original = AccountStore._apply_policy_schema.__func__

    def fail_on_tenant(cls, conn):
        original(cls, conn)
        conn.execute(
            sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(table_name))
        )
        conn.execute(
            "INSERT INTO project_quota(project_id) VALUES (%s)", (table_name,)
        )
        raise RuntimeError("injected multi-schema migration failure")

    monkeypatch.setattr(AccountStore, "_apply_policy_schema", classmethod(fail_on_tenant))
    with pytest.raises(RuntimeError, match="multi-schema migration failure"):
        accounts.migrate()

    with accounts.connect() as conn:
        for schema_name in ("public", project_schema):
            assert conn.execute(
                "SELECT to_regclass(%s) AS table_name",
                (f"{schema_name}.{table_name}",),
            ).fetchone()["table_name"] is None
            assert conn.execute(
                sql.SQL("SELECT count(*) AS n FROM {}.project_quota WHERE project_id = %s")
                .format(sql.Identifier(schema_name)),
                (table_name,),
            ).fetchone()["n"] == 0

    monkeypatch.setattr(AccountStore, "_apply_policy_schema", classmethod(original))
    accounts.migrate()
    with accounts.connect() as conn:
        revisions = {
            row["component"]: row["version"]
            for row in conn.execute("SELECT component, version FROM schema_revision").fetchall()
        }
        assert revisions["control"] == BASE_SCHEMA_VERSION
        assert revisions["policy"] == POLICY_SCHEMA_VERSION
        assert revisions["accounts"] == ACCOUNT_SCHEMA_VERSION
