"""Native isolated-cluster tests (opt-in via CLASHBOLT_TEST_TARGET_ADMIN_URL)."""

import os
from uuid import uuid4

import psycopg
import pytest

from clashbolt.target_db import disposable_target, validate_target_cluster


@pytest.fixture
def cluster():
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    if not admin or not control:
        pytest.skip("Separate native PostgreSQL cluster is required")
    return admin, control


def test_same_cluster_and_remote_are_rejected():
    same = "postgresql://user:pwd@127.0.0.1:5432/testing"
    with pytest.raises(ValueError, match="independent"):
        validate_target_cluster(same, same)
    with pytest.raises(ValueError, match="independent"):
        validate_target_cluster(
            "postgresql://user:pwd@198.51.100.1:5433/testing", same
        )


def test_target_database_is_per_run_and_is_dropped(cluster):
    admin, control = cluster
    first, second = uuid4(), uuid4()
    with disposable_target(admin, control, first) as a:
        with disposable_target(admin, control, second) as b:
            assert a.database != b.database
            assert a.role != b.role
            with psycopg.connect(a.url) as first_db:
                first_db.execute(
                    "UPDATE inventory SET confirmed_count = 1 WHERE id = 1"
                )
            with psycopg.connect(b.url) as second_db:
                row = second_db.execute(
                    "SELECT remaining, confirmed_count FROM inventory"
                ).fetchone()
                assert row == (1, 0)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    second_db.execute("DROP TABLE inventory")
        with psycopg.connect(a.url) as still_active:
            assert still_active.execute(
                "SELECT confirmed_count FROM inventory"
            ).fetchone()[0] == 1
    # The target database must no longer exist after the context exits.
    with psycopg.connect(admin, autocommit=True) as connection:
        names = connection.execute(
            "SELECT datname FROM pg_database WHERE datname IN (%s, %s)",
            (f"cb_target_{first.hex}", f"cb_target_{second.hex}"),
        ).fetchall()
        assert names == []


def test_exception_still_drops_target(cluster):
    admin, control = cluster
    run_id = uuid4()
    with pytest.raises(RuntimeError, match="synthetic fault"):
        with disposable_target(admin, control, run_id):
            raise RuntimeError("synthetic fault")
    with psycopg.connect(admin, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s",
            (f"cb_target_{run_id.hex}",),
        ).fetchone()[0] == 0
