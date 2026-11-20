"""Provision a fresh *local* PostgreSQL database for a shipped synthetic target.

Requires a separately administered native target cluster.  Never reuse the
control PostgreSQL cluster and never provision a network-supplied target.
A database boundary is not a process or network sandbox.
"""

import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def _local_address(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def validate_target_cluster(target_admin_dsn: str, control_dsn: str) -> None:
    """Enforce a separate native port and loopback-only target cluster."""
    admin = conninfo_to_dict(target_admin_dsn)
    control = conninfo_to_dict(control_dsn)
    host = admin.get("host", "")
    target_port = int(admin.get("port") or 5432)
    control_port = int(control.get("port") or 5432)
    if (
        not _local_address(host)
        or not _local_address(control.get("host", ""))
        or (host in ("localhost", "127.0.0.1", "::1")
            and control.get("host") in ("localhost", "127.0.0.1", "::1")
            and target_port == control_port)
    ):
        raise ValueError(
            "Target requires an independent loopback PostgreSQL cluster and port"
        )


@dataclass(frozen=True)
class TargetLease:
    database: str
    role: str
    url: str



def drop_abandoned_target(target_admin_dsn: str, control_dsn: str, instance_id: UUID) -> None:
    """Idempotently discard only an instance address derived from a UUID."""
    validate_target_cluster(target_admin_dsn, control_dsn)
    dbname = f"cb_target_{instance_id.hex}"
    role = f"cb_runner_{instance_id.hex}"
    with psycopg.connect(
        target_admin_dsn, autocommit=True, connect_timeout=5
    ) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(dbname)
            )
        )
        connection.execute(
            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
        )


@contextmanager
def disposable_target(target_admin_dsn: str, control_dsn: str, run_id: UUID):
    """One run, one database, one least-privilege role, and forced teardown.

    The worker holds cluster administration privileges; only the fixed child
    fixture receives the restricted role DSN. These credentials are never
    returned from the FastAPI endpoints or persisted in experiment rows.
    """
    validate_target_cluster(target_admin_dsn, control_dsn)
    dbname = f"cb_target_{run_id.hex}"
    role = f"cb_runner_{run_id.hex}"
    password = secrets.token_urlsafe(32)
    admin = psycopg.connect(target_admin_dsn, autocommit=True, connect_timeout=5)
    role_created = False
    database_created = False
    try:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        role_created = True
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname))
        )
        database_created = True
        admin.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(dbname)
            )
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), sql.Identifier(role)
            )
        )
        with psycopg.connect(
            make_conninfo(target_admin_dsn, dbname=dbname), connect_timeout=5
        ) as setup:
            setup.execute(
                """
                CREATE TABLE inventory(
                    id INTEGER PRIMARY KEY,
                    remaining INTEGER NOT NULL,
                    confirmed_count INTEGER NOT NULL
                )
                """
            )
            setup.execute(
                "INSERT INTO inventory(id, remaining, confirmed_count) VALUES (1, 1, 0)"
            )
            setup.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
            )
            setup.execute(
                sql.SQL("GRANT SELECT, UPDATE ON inventory TO {}").format(
                    sql.Identifier(role)
                )
            )
        url = make_conninfo(
            target_admin_dsn, dbname=dbname, user=role, password=password
        )
        yield TargetLease(database=dbname, role=role, url=url)
    finally:
        try:
            if database_created:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )
        finally:
            try:
                if role_created:
                    admin.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                    )
            finally:
                admin.close()
