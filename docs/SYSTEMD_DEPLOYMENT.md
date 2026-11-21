# Native Linux systemd hardening examples

These units are **deployment examples, not production certification**.
They only run the shipped synthetic fixture. No Docker, containers, or arbitrary
external test programs are used. Install native Python and PostgreSQL first.

The service templates are at `deploy/systemd/clashbolt-api.service` and
`deploy/systemd/clashbolt-worker.service`. They expect:

- Application and installed Python venv under `/opt/clashbolt`
- Two distinct dedicated local OS users: `clashbolt-api` and
  `clashbolt-worker`, with no interactive login
- Root-managed environment files `/etc/clashbolt/api.env` and
  `/etc/clashbolt/worker.env`, each readable only by its own service group
- A native PostgreSQL control database and a **different** local cluster for
  disposable targets, when native target mode is enabled

The **API environment** contains `CLASHBOLT_DATABASE_URL`,
`CLASHBOLT_ACCESS_MODE=accounts`, a fresh
`CLASHBOLT_SESSION_SECRET`, and optionally a nonsecret endpoint such as
`CLASHBOLT_TARGET_CLUSTER_URL=postgresql://127.0.0.1:5433/postgres`.
It must **not** contain `CLASHBOLT_TARGET_ADMIN_URL`.

The **worker environment** contains its own control database DSN and, if
native targets are enabled, the credential-bearing
`CLASHBOLT_TARGET_ADMIN_URL`. The API must not share that secret.
Native replay and reduction use version-fenced `verification_job` work
submitted to the control database. The independent worker executes verification
using its target-admin credential and persists a result that the API can read.
For native runs, the React desk uses this queue instead of making API-side
target connections. Legacy synchronous API replay/reduction endpoints should
not be relied on in an API instance without target-admin credentials.

Both units bind or communicate only on loopback and include systemd
`IPAddressDeny=any` plus `IPAddressAllow=localhost`, along with CPU/memory
and privilege containment. Those IP directives require a supporting Linux
kernel/systemd cgroup BPF configuration. Check with your administrator that
they are **actually enforced** on the deployment machine, not merely present
in unit files. An unenforced directive is not an egress boundary.

Example review commands for an administrator:

```bash
sudo systemd-analyze security clashbolt-api.service
sudo systemd-analyze security clashbolt-worker.service
sudo systemctl show clashbolt-api.service -p IPAddressDeny -p IPAddressAllow
sudo systemctl show clashbolt-worker.service -p IPAddressDeny -p IPAddressAllow
```

Do not expose the API beyond `127.0.0.1`. The Vite development server is
not a production reverse proxy. Do not load untrusted targets into the worker.
Before considering deployment complete, conduct independent deny-egress
validation, PostgreSQL backup/restore, crash recovery, identity review, and
resource-exhaustion tests on the actual host.

These unit files have static CI checks; they have **not** been executed
inside the target machine's systemd or subjected to a kernel-enforced
network-denial penetration test.

## Per-request fixed target service

The worker unit requires `clashbolt-target.socket`. Install that socket and
`clashbolt-target@.service` along with the worker unit on a cgroup-v2 Linux
host with systemd mount namespaces, seccomp and dynamic users. Each accepted
connection starts one bounded fixture command under its own dynamic identity.
The worker group has access to the root-owned socket. Each target has a private
network namespace, AF_UNIX only, an empty root directory, read-only runtime
mounts and a single PostgreSQL socket mount. Keep the bound runtime directories
free of secrets and service sockets. The target receives only a generated
instance ID and its disposable password for a native trial.

Create an empty root-owned directory `/var/lib/clashbolt-target-root` (0755).
Build a dedicated root-owned, read-only-to-services Python venv at
`/opt/clashbolt-fixture` using the distribution Python under /usr. Install a
reviewed **non-editable** ClashBolt wheel and its dependencies there. Install
the units, reload systemd and start the target socket before the worker.
Never place environment files or the checkout inside the target root or venv.

The native unit expects the disposable cluster at port 5433 with its Unix
socket at `/run/postgresql/.s.PGSQL.5433`. Enable SCRAM password authentication
for its disposable roles on that socket. On PostgreSQL 16+ the target cluster's
first matching pg_hba.conf rule can be `local all /^cb_runner_ scram-sha-256`.
The runtime libpq must support require_auth. The control cluster and its socket
must be separate. Memory jobs can run without the PostgreSQL socket. Missing
service, incompatible authentication or stale socket mounts fail the request.
The deployed worker never retries a job under its own identity.

The unit caps each instance at 50% of one CPU, 3 CPU seconds, 384 MiB memory,
8 tasks, 64 file descriptors and 8 seconds of runtime. PostgreSQL server cost
is outside these cgroups. Local development retains its existing subprocess
mode. API scratch demos remain trusted in-process code. Native verification
replay and reduction provision from the worker, and run fixture operations in
the target unit. Worker-queued replay and reduction now persist each disposable target UUID
and verification lease generation before provisioning. Expired verification
claims can be reclaimed by the worker's idempotent native database/role
cleanup, provided its administrator credential is configured. Standalone
CLI verifications are not tracked this way. Apply control schema revision 6
using the normal migration before starting updated workers. This is not a
power-loss or network-partition certification. Use disposable installations
for the CI host drill in `ci/check-target-service.sh`; it configures that
machine's services and HBA.
