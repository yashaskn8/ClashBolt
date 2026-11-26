"""Provision a fresh *local* PostgreSQL database for a shipped synthetic target.

Requires a separately administered native target cluster.  Never reuse the
control PostgreSQL cluster and never provision a network-supplied target.
A database boundary is not a process or network sandbox.
"""

import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import ip_address
from time import perf_counter
from typing import Callable
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


def _local_cluster_port(dsn: str) -> int:
    """Validate the effective libpq endpoint, including routing overrides."""
    values = conninfo_to_dict(dsn)
    host = values.get("host", "")
    hostaddr = values.get("hostaddr") or os.getenv("PGHOSTADDR", "")
    if (
        not _local_address(host)
        or (hostaddr and not _local_address(hostaddr))
        or values.get("service")
        or os.getenv("PGSERVICE")
    ):
        raise ValueError("Target requires explicit loopback-only PostgreSQL endpoints")
    try:
        port = int(values.get("port") or os.getenv("PGPORT") or "5432")
    except (TypeError, ValueError) as error:
        raise ValueError("Target requires a single numeric local PostgreSQL port") from error
    if not 1 <= port <= 65535:
        raise ValueError("Target requires a valid local PostgreSQL port")
    return port


def validate_target_cluster(target_admin_dsn: str, control_dsn: str) -> None:
    """Require distinct native ports, even for loopback aliases/hostaddr."""
    target_port = _local_cluster_port(target_admin_dsn)
    control_port = _local_cluster_port(control_dsn)
    if target_port == control_port:
        raise ValueError(
            "Target requires an independent loopback PostgreSQL cluster and port"
        )


@dataclass(frozen=True)
class TargetLease:
    database: str
    role: str
    url: str = field(repr=False)
    _sampler: Callable[[], dict | None] | None = field(
        default=None, repr=False, compare=False
    )
    provision_ms: float | None = None

    def sample_stats(self) -> dict | None:
        return self._sampler() if self._sampler is not None else None



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
    provision_start = perf_counter()
    admin = psycopg.connect(target_admin_dsn, autocommit=True, connect_timeout=5)
    role_created = False
    database_created = False
    try:
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN CONNECTION LIMIT 2 PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        role_created = True
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ALLOW_CONNECTIONS false").format(
                sql.Identifier(dbname)
            )
        )
        database_created = True
        admin.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(dbname)
            )
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(dbname), sql.Identifier(role)
            )
        )
        admin.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(dbname))
        )
        with psycopg.connect(
            make_conninfo(target_admin_dsn, dbname=dbname), connect_timeout=5
        ) as setup:
            setup.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
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
                sql.SQL(
                    "GRANT SELECT, UPDATE (remaining, confirmed_count) ON inventory TO {}"
                ).format(sql.Identifier(role))
            )
        # Build a new credential from the connected endpoint. Administrator
        # options such as sslkey, sslpassword, passfile and options stay private.
        parameters = admin.info.get_parameters()
        transport = {
            key: parameters[key]
            for key in (
                "sslmode", "sslrootcert", "ssl_min_protocol_version",
                "ssl_max_protocol_version", "channel_binding", "gssencmode",
                "require_auth", "sslcrl", "sslcrldir",
            )
            if key in parameters
        }
        url = make_conninfo(
            host=admin.info.host, hostaddr=admin.info.hostaddr, port=admin.info.port,
            dbname=dbname, user=role, password=password, **transport,
        )
        def _sample_stats() -> dict | None:
            try:
                admin.execute("SELECT pg_stat_force_next_flush()")
                row = admin.execute(
                    """
                    SELECT active_time, xact_commit, xact_rollback,
                           blks_hit, blks_read, pg_database_size(datname) AS database_bytes
                    FROM pg_stat_database WHERE datname = %s
                    """,
                    (dbname,),
                ).fetchone()
            except psycopg.Error:
                return None
            if not row or any(value is None for value in row):
                return None
            return {
                "active_ms": round(max(0.0, float(row[0])), 3),
                "xact_commit": int(row[1]),
                "xact_rollback": int(row[2]),
                "blks_hit": int(row[3]),
                "blks_read": int(row[4]),
                "database_bytes": int(row[5]),
            }

        admin.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role)))
        provision_ms = round(max(0.0, (perf_counter() - provision_start) * 1000), 3)
        yield TargetLease(
            database=dbname, role=role, url=url,
            _sampler=_sample_stats, provision_ms=provision_ms,
        )
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
