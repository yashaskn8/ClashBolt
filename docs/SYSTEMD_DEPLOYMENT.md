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
