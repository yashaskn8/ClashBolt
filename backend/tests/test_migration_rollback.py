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
