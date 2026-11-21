"""Run only as ExecStartPre in the deployed unit on an ephemeral CI host."""

import errno
import os
import resource
import socket
from pathlib import Path

assert os.geteuid() != 0
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (64, 64)
assert max(resource.getrlimit(resource.RLIMIT_CPU)) <= 3
assert "CLASHBOLT_TARGET_ADMIN_URL" not in os.environ
assert "CLASHBOLT_DATABASE_URL" not in os.environ
assert Path("/run/clashbolt-db/.s.PGSQL.5433").is_socket()
for path in (
    "/etc/clashbolt-host-canary", "/run/clashbolt-target.sock",
    "/run/postgresql/.s.PGSQL.5432", "/proc/1/environ",
):
    assert not Path(path).exists(), path
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        connection = socket.socket(family, socket.SOCK_STREAM)
    except OSError as error:
        assert error.errno in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES)
    else:
        connection.close()
        raise AssertionError("Target could open an IP socket")
