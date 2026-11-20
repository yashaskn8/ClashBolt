# Native loopback deployment (Linux, no Docker)

This layout keeps ClashBolt **local-only**. It is not an Internet-facing
multi-tenant deployment and does not make arbitrary target code safe.

## Build

Install Python 3.12+, PostgreSQL and Node 22+ on a dedicated Linux host.
Place a reviewed copy of the repository in `/opt/clashbolt`. Build the
frontend **once** with `cd frontend && npm ci && npm run build`, install
Python in `/opt/clashbolt/.venv` and run schema migrations with a separate,
trusted administration credential. The runtime API serves the resulting
`frontend/dist` assets itself, from the same origin as `/api`.
Vite is not run as a deployment service.

## Separate service credentials

Create two restricted, non-login OS identities named `clashbolt-api` and
`clashbolt-worker` (each with its own group), grant both read-only access
to the installed application, and place separate root-managed environment
files at `/etc/clashbolt/api.env` and `/etc/clashbolt/worker.env` with
permissions `0600` and the appropriate service group access. Use a
trusted system administrator to configure the native PostgreSQL account
permissions and protect the credentials.

The **API environment** requires `CLASHBOLT_ACCESS_MODE=accounts`,
`CLASHBOLT_DATABASE_URL`, a random `CLASHBOLT_SESSION_SECRET` of at
least 32 characters, and `CLASHBOLT_FRONTEND_DIST=/opt/clashbolt/frontend/dist`.
It must **not** contain the target PostgreSQL administrator credential.

The **worker environment** contains `CLASHBOLT_DATABASE_URL` and, only
when native target mode is enabled, `CLASHBOLT_TARGET_ADMIN_URL` pointing
at a separate native target cluster. These secrets must not be committed.

Copy the two systemd unit templates from `deploy/systemd` into your normal
systemd unit directory and perform `systemctl daemon-reload`, then enable
and start them after verifying the OS users, database grants and environment
file permissions. The API binds only to `127.0.0.1:8000`.
Use `systemd-analyze security`, `systemctl status`, and the
`/api/ready` endpoint to check the deployed services.

The unit templates use a read-only filesystem view, private temp/devices,
separate OS users, cgroup limits and systemd network policy allowing only
loopback connectivity. This is useful **defense in depth**, but allowing
loopback does not isolate all host-local services. Systemd policies depend
on the actual kernel, cgroup and Linux configuration and must be tested
on the target host. There is no claim that these units securely sandbox
untrusted application code.

## Release checks

Build/test with `python -m ruff check .`, `python -m pytest -q`,
`npm run build`, and the CI browser checks. Confirm the production
`/api/ready` status before and after restart. Verify PostgreSQL backup
and disposable restore drills using the backup guide. Keep the API private
to the trusted machine; never expose it directly through a public proxy.
