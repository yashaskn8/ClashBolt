# Native ClashBolt setup (no Docker)

**Security boundary:** run ClashBolt only on a trusted developer machine, with the
FastAPI process bound to `127.0.0.1`. This is a research lab, not a hosted service.
There is no multitenant authentication or external target isolation.

## 1. Backend and frontend

Install Python 3.11+, Node 20+, and native PostgreSQL 16+.

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On Linux/macOS, activate with `source .venv/bin/activate`.

From `frontend`:

```powershell
npm install
npm run dev
```

The Vite development server listens on `http://127.0.0.1:5173`
and forwards `/api` to the local FastAPI process.

## 2. Native PostgreSQL

Create a dedicated local database and restricted database user using your native
PostgreSQL administration tools (not a production database). For example, from
an administrator `psql` session:

```sql
CREATE USER clashbolt WITH PASSWORD 'choose-a-strong-local-password';
CREATE DATABASE clashbolt OWNER clashbolt;
```

Configure the connection URL as an environment variable in PowerShell:

```powershell
$env:CLASHBOLT_DATABASE_URL = "postgresql://clashbolt:YOUR_PASSWORD@127.0.0.1:5432/clashbolt"
$env:CLASHBOLT_OPERATOR_TOKEN = "A_LONG_RANDOM_LOCAL_SECRET"
$env:CLASHBOLT_REVIEWER_TOKEN = "A_DIFFERENT_LONG_RANDOM_REVIEWER_SECRET"
$env:CLASHBOLT_ACCESS_MODE = "private"
python -m clashbolt.policy
python -m clashbolt.worker
```

`python -m clashbolt.policy` is **required** for durable mode. It applies the
PostgreSQL run, approved-oracle, capability and receipt migrations. Schema is
not silently created by API startup or workers.

In a second terminal sharing the environment variables:

```powershell
uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000
```

Create and approve the shipped demo oracle using the operator console before
submitting durable experiments; the selected exact approval ID is bound to each
accepted run. Revoking a rule withdraws certified verdicts while retaining
historical witness data.

The API accepts approved experiments into the PostgreSQL queue. The **separate worker**
process claims and executes each accepted synthetic run. Its subprocess receives
a minimized environment, not the control database or operator secrets. API requests do not
silently spawn durable background work.

If the database variable is absent, the UI uses its in-memory scratch mode
instead, with no persistence.

## 3. Verify

```powershell
python -m ruff check .
python -m pytest -q
```

Integration tests require a dedicated, disposable database:

```powershell
$env:CLASHBOLT_TEST_DATABASE_URL = "postgresql://clashbolt:YOUR_PASSWORD@127.0.0.1:5432/clashbolt_test"
python -m pytest -q
```

**Never point tests at a non-test database:** the integration fixtures truncate
test tables. GitHub Actions creates an ephemeral native PostgreSQL database and
runs those tests automatically.

Export a witness using the UI, then verify it in a different Python process:

```powershell
python -m clashbolt.witness .\clashbolt-synthetic-RUN_ID.json
```

The verifier accepts only shipped synthetic fixture contracts and checks
operation history and evidence hashes after replaying the same application gate order
on fresh state. For native PostgreSQL witnesses, it needs the separate target cluster.
This does **not** prove a real-world or uncontrolled concurrency bug.

## 4. What the operator and reviewer tokens protect

`CLASHBOLT_OPERATOR_TOKEN` is required for durable run submission,
cancellation and replay as well as oracle approval, capability changes, audit
inspection and planner evaluation. Send it in the `X-Operator-Token` header from a trusted
local client. Do not commit it or expose it in a URL.

Only POST durable mutations are protected by this token: the read-only run
index, snapshots and synthetic scratch endpoints have no identity checks.
Keep the API on loopback and never put secrets in URLs.

A separate `CLASHBOLT_REVIEWER_TOKEN` credential is used for approval and
revocation when configured. Private mode refuses approval if no distinct
reviewer credential is configured. Do not put either secret in source control.

The token is a single-machine operator control, not identity verification,
multitenant RBAC, an independent human review system, or production authorization.
The planner only proposes deterministic plans; it cannot mint tokens or change
the checker. Tool execution accepts solely the built-in fixture and approved
variant/schedule pair. Revocation prevents future calls but cannot undo work
already started.

## 5. Target-process boundary

In PostgreSQL mode, the worker launches only the packaged
`clashbolt.fixture_runner` module as a short-lived child process with a timeout.
No arbitrary target URL, script or command is accepted. The parent validates the
returned contract, evidence hash, checkpoint order and oracle verdict before
accepting control-plane results. This prevents a malformed fixture response from
silently certifying a finding.

**Important:** Process separation is not an OS sandbox or network-egress
firewall. Do not substitute untrusted application code for the shipped fixture
until proper process/account/network isolation and disposable target databases
have been implemented and independently tested.


## 6. Reproducible baseline evaluation

Run the deterministic comparison locally, without any model API calls:

```powershell
python -m clashbolt.planner --trials 8
```

The JSON report binds seeds, the synthetic fixture and the oracle SHA-256,
counts controlled failures, and records per-trial evidence hashes under an equal
gate budget. It explicitly returns `promotion_eligible: false`. These runs
cannot establish held-out performance, statistical advantage, or AI safety.


## 7. Disposable native PostgreSQL targets

The control database and **target databases must use separate native clusters**
on different loopback ports. Do not point this at a production cluster.

For a native Ubuntu PostgreSQL installation, provision a second PostgreSQL
cluster on port 5433 using your native PostgreSQL administration tools. The
dedicated target administrator needs permission to create/drop local databases,
create/drop restricted roles, and initialize the fixed inventory table. Restrict
its credentials to the worker machine; the child process receives only a fresh
per-run role credential.

Set the environment variable in every worker and verifier process:

```powershell
$env:CLASHBOLT_TARGET_ADMIN_URL = "postgresql://TARGET_ADMIN:TARGET_PASSWORD@127.0.0.1:5433/postgres"
```

The React composer then enables **Fresh isolated native PostgreSQL database**.
A fresh restricted database is created for a run; after execution the worker
drops it. Generation-bound target records allow another worker to reclaim
leftovers after lease expiry, including simulated unclean worker death.
There is no OS-account sandbox or deny-egress firewall yet. Do not run untrusted
target programs.

## 8. Private local operator session

Setting `CLASHBOLT_ACCESS_MODE=private` protects read endpoints and SSE with a
one-hour HTTP-only, SameSite=Strict operator session. Use **Unlock private API**
after entering the local operator token. Mutations still require the token
header, to avoid cookie-only CSRF authority. The reviewer has a distinct secret
for oracle approval. This is **not multitenant login, RBAC or TLS termination**;
reverse-proxy exposure remains unsafe.

## 9. Optional local AI schedule suggestions

You may separately install and run an Ollama-compatible local model server
listening on `http://127.0.0.1:11434`. Configure:

```powershell
$env:CLASHBOLT_LOCAL_MODEL = "YOUR_INSTALLED_LOCAL_MODEL_NAME"
```

The **Suggest local model plan** button makes one bounded JSON request. The
backend validates it against only two shipped synthetic schedules, fixed target,
fixed conflict edge, and fixed four-gate budget. Model errors or invalid JSON
produce an explicitly labeled deterministic fallback. No model can approve
an invariant or mint capabilities.

The local model service is **not configured or quality-tested in CI**. Its
output is labeled unpromoted; benchmark superiority still requires frozen
external held-out targets and total-cost paired trials.


## 10. Versioned migration and saved experiment drafts

`python -m clashbolt.policy` applies the control ledger and policy schema in
one serialized PostgreSQL transaction, recording their schema revisions. A
database newer than the installed application's revision is rejected. Before
upgrading an existing installation, make a verified PostgreSQL backup; this
repository does not yet ship automatic rollback or disaster-recovery automation.

The React composer can save and reopen the selected implementation, schedule
and execution backend. Optimistic version checks reject concurrent stale
updates. Saved drafts never embed operator tokens, reviewer credentials, live
capabilities or rule-approval authority.

## 11. Model call budgets and evidence-ranked search

Optional local model calls reserve up to 256 generated tokens **before** the
HTTP request, with a default daily limit of 20 calls/5,120 reserved tokens.
The budget uses an atomic PostgreSQL row, not process-local counters. Observed
model usage is recorded when the adapter returns a valid receipt; timeouts or
unknown usage retain the full reservation conservatively. The operator can
inspect current reservations with `GET /api/agent/model-budget` using the
operator header.

The **semantic frontier** ranks only the shipped shared-inventory overlap/serial
schedules from independently verified persisted plan receipts, using a
deterministic exploration penalty. Selecting a frontier proposal does not
execute a tool or grant capability. This is not an independently validated
semantic AI improvement.

The constrained invariant workshop records a source-bound fixed-rule
candidate and three deterministic PASS/FAIL/UNKNOWN test vectors. Reviewer
approval is still needed. Freeform user/model predicates or Python evaluation
are deliberately unsupported.

## 12. Portable evidence ZIP packages

For currently certified durable witnesses, choose **Export verifier bundle
(ZIP)**. The archive contains only `witness.json`, `manifest.json` and
`README.txt`. It has no executable target or secrets. After installing the
same ClashBolt Python package in another clean environment, verify using:

```powershell
python -m clashbolt.witness_bundle verify .\clashbolt-witness-RUN_ID.zip
```

The verifier checks archive bounds, member names, manifest bindings, evidence
history and the fresh synthetic replay. Native database witnesses require the
matching native test-cluster environment. A reproduced synthetic fixture is not
proof of an ungated or independently deployed application vulnerability.


### Independent reviewer-only approval

In private mode, a reviewer can approve or revoke an existing immutable
proposal by presenting only `X-Reviewer-Token` to the corresponding
`/api/oracle/proposals/{id}/approve` or `/revoke` endpoint. No operator
session or operator credential is required for those two actions. All other
private read endpoints still need the operator session, and writes still
require the operator credential or a scope-limited tool capability.

This separates credentials for review and execution, but is not yet proof
of different human identities or a project-level RBAC system.


## 13. Named accounts and backward-compatible local project access

The shared-token `private` mode remains supported for a single-machine lab.
For a stronger local separation of review and execution, use **named accounts**
instead. This mode has no public HTTP registration, default passwords, or
passwords in environment variables. All credentials are provisioned by a
trusted machine/database administrator from the local CLI.

1. Set `CLASHBOLT_DATABASE_URL` to the trusted control PostgreSQL database.
2. Apply the schema with `python -m clashbolt.accounts migrate`.
3. Create individual accounts using the terminal commands below. Each command
   prompts twice for a password (at least 16 characters), and never prints it.
4. Set a fresh secret of **at least 32 characters** in the application runtime:
   `CLASHBOLT_SESSION_SECRET`. Do not commit it or reuse a password.
5. Set `CLASHBOLT_ACCESS_MODE=accounts`, restart the API, and sign in through
   the React composer.

```powershell
python -m clashbolt.accounts create experiment-operator operator
python -m clashbolt.accounts create independent-reviewer reviewer
python -m clashbolt.accounts create read-only-user viewer
$env:CLASHBOLT_ACCESS_MODE = "accounts"
$env:CLASHBOLT_SESSION_SECRET = "REPLACE_WITH_FRESH_32_PLUS_CHAR_SECRET"
```

In account mode, operators may create proposals, plans, scoped capabilities,
synthetic experiments and run cancellations; reviewers may only approve/revoke
existing oracle proposals; viewers may inspect available read-only API data.
The backend records the **authenticated account name**, ignoring identity
labels supplied in request JSON. Cookies are signed, HTTP-only, SameSite=Strict,
expire after one hour, and are revalidated against the active account on each
request. Password-derived hashes, failed-login lockout, and revocable account
sessions are persisted in PostgreSQL.

Account login now reserves a PostgreSQL-backed, one-minute request budget of
20 attempts per direct loopback peer before performing a password hash check.
Requests exceeding this limit return HTTP 429 and a Retry-After header. This is
**not** an Internet-facing per-user or proxy-aware rate limiter: do not trust
`X-Forwarded-For`, enable proxy exposure, or assume separate clients behind a
shared gateway are independently limited. Existing username lockout remains.
After updating the application, run `python -m clashbolt.accounts migrate`
to apply account schema revision 5 before restarting the API.

A reviewer can select a pending proposal in the React console after signing in
without possessing an operator credential. An operator can then sign back in
to execute the separately approved proposal.

Named accounts now receive a durable grant for the existing `local`
project. Every protected API request resolves this project role directly from
PostgreSQL, so an existing signed cookie does not preserve access after a
grant is revoked. A project role cannot exceed the account's provisioned
viewer/operator/reviewer role: a reviewer can never grant themselves operator
authority. These grants and changes have append-only audit rows.

The following **trusted administrator CLI** commands allow a local grant
to be removed or restored (they are not public API routes):

```powershell
python -m clashbolt.accounts revoke-local experiment-operator
python -m clashbolt.accounts grant-local experiment-operator operator
python -m clashbolt.accounts grant-local experiment-operator viewer
```

Account schema revision **5** adds an issuer-account and issuer-project
binding to each newly issued named-account tool capability. The capability
broker rechecks current operator membership under database row locks before
debiting a one-use token; a legacy token cannot be carried into named-account
mode. Logout, password reset, account disablement, membership removal and
operator-to-viewer downgrade permanently revoke already-issued account-bound
capabilities. Re-granting operator access does not resurrect old bearer tokens.
Native authorization and a real Chromium role-change test cover these
boundaries; they do **not** amount to general multi-project tool isolation.

The React workspace re-reads the effective project role at two-second
intervals. A downgrade removes operator UI controls; a revoked session or
membership clears the current workspace selection and requires sign-in.
Database authorization still takes effect immediately, independently of
the browser refresh interval.

An account with no `local` grant cannot sign into the current application
or access its project resources. The `migrate` command backfills grants for
existing accounts **only once**, when upgrading from schema revision 3. Later
migrations must not restore a deliberately revoked grant. Grant changes are
checked on every request; a signed-out or disabled account remains invalid.
This is still a **single supported project**, not general multitenancy: other
project IDs are not accepted by API resource endpoints until every query and
worker boundary is separately scoped and tested.

To revoke an account and invalidate its sessions immediately, run:

```powershell
python -m clashbolt.accounts disable experiment-operator
```

To reset a password and invalidate existing sessions:

```powershell
python -m clashbolt.accounts reset independent-reviewer
```

The original **local** grant is a single-project compatibility path; the
multi-project CLI workflow appears below. This is not SSO,
multi-organization tenancy, production TLS termination, device trust, or a
verified two-person approval process. Do not make the API Internet-facing.


## 14. Native verification without API database-administrator credentials

For a native PostgreSQL experiment, the React witness desk submits a typed
`REPLAY` or `REDUCE` verification job to:
`POST /api/runs/{run_id}/verification-jobs`, with a JSON body such as
`{"kind":"REPLAY"}`. Poll `GET /api/verification-jobs/{job_id}` to
inspect the authoritative `PENDING`, `RUNNING`, `COMPLETE` or `UNKNOWN`
result. The independent worker processes the job using its separately held
target-cluster administrator credential.

The API should receive only the **non-secret**
`CLASHBOLT_TARGET_CLUSTER_URL`, while the worker has
`CLASHBOLT_TARGET_ADMIN_URL`. Verification completion is bound to the
current version of the approved experiment; withdrawal of the oracle makes
previous results `UNKNOWN`. A missing worker receipt also yields `UNKNOWN`
rather than silently retrying an uncertain effect.

The legacy default is a **single-project native worker queue**; accounts
mode rotates across registered isolated project queues. Neither is a distributed exactly-once
effect guarantee or an externally verified finding.


## 15. Operational status and request boundaries

`GET /api/health` is process liveness only. `GET /api/ready` checks native
PostgreSQL connectivity, expected schema revisions, and configured active
accounts in named-account mode. For worker availability use the **operator-only**
`GET /api/operations`: it shows responsive worker heartbeats, pending run
count and native verification queue depth. The React recovery index shows the
same snapshot to an authorized operator. Heartbeats older than 15 seconds are
reported as stale; an active API is not evidence of an active worker.

Browser writes are restricted to the documented local origins; unexpected
`Host` headers and non-loopback peers are rejected, and API data is marked
`Cache-Control: no-store`. These defenses do not replace actual host-enforced
network egress rules, project tenancy, TLS, or a penetration test. Do not
expose the synthetic lab beyond a trusted local machine.


## Native control-plane database archives

Create a new PostgreSQL custom archive from a dedicated loopback control
database. The backup command never overwrites an existing archive, passes the
database password through an environment variable rather than process
arguments, and checks the archive with `pg_restore` before publishing it:

```powershell
python -m clashbolt.backup .\clashbolt-control-2026-11-20.dump
```

Keep the `.dump` and matching `.sha256.json` files in encrypted, access-limited
storage. **They contain sensitive account records and stored evidence.** A
successful archive integrity check is not a successful restore. Test restoration
only into a newly created *disposable* PostgreSQL database using native
administration tools and verify the application readiness endpoint. Never
automatically restore over a running control database.


### Disposable restore drill

A full restore test requires an independently administered, local target
PostgreSQL cluster (not the control cluster). The function
`clashbolt.backup.verify_disposable_restore(archive, control_dsn, target_admin_dsn)`
accepts only a hash-verified archive and **always generates a fresh database
name**. It restores there, verifies the schema revisions, and drops the
scratch database. There is deliberately no restore-to-existing-database API.

GitHub Actions exercises the restore drill against the two ephemeral native
PostgreSQL clusters. Passing this test does not replace off-site encrypted
backups, recovery-time objectives or a full disaster-recovery rehearsal.


## Local operational metrics

A token-protected `GET /api/metrics` endpoint exports bounded Prometheus
text with only known run/job state labels and aggregate worker health. No run
IDs, user names, passwords or witness contents appear in metrics. For single
operator local mode, supply `X-Operator-Token` to the local endpoint. In named
account mode, it requires an authenticated operator session.

The metrics are gauges/snapshots only, not billing, alerting or a hosted
monitoring service. Do not expose this development API through a public proxy.


## Versioned agent decision history

New agent plans record append-only, SHA-256 linked decisions in PostgreSQL
when proposed, authorized for dispatch, and finalized with a verified
receipt or UNKNOWN/timeout classification. Follow-up proposals and oracle
revocations are recorded in the same transactions as their state changes.
The operator can inspect a plan's event chain using
`GET /api/agent/plans/PLAN_UUID/history` with the local operator credential
or authenticated operator session, or the review-console button.

A `VERIFIED` history confirms internal event-chain consistency, **not**
external application behavior or resistance to the PostgreSQL administrator.
Older pre-migration plans without full history report `UNCONFIRMED`.

After applying current policy schema revision **10** (which includes the
revision 9 integrity sweep) through the usual
`python -m clashbolt.policy` or account migration, the worker periodically
checks each active plan's stored context against its history. A missing or
inconsistent history is quarantined as terminal `UNKNOWN`, with a persistent
evidence gap. The worker never repeats an uncertain tool call. Once
quarantined, a plan cannot mint another scoped capability or propose an
adaptive follow-up. Review the original record with
`GET /api/agent/plans/PLAN_UUID/history` and inspect
`GET /api/agent/plans/PLAN_UUID/gaps`; create a new independently approved
plan rather than editing the corrupt record.


## Named-account draft boundaries

When `CLASHBOLT_ACCESS_MODE=accounts`, each authenticated account has its
own version-checked experiment draft. Switching or signing out of an account
clears the previous form and restores only the next account's saved settings.
The account-specific records have a foreign key to the provisioned account
and disappear when that account is deleted by a trusted database
administrator. Existing local/private single-operator modes continue using
the local shared draft.

Drafts never grant oracle approval, tool capability, target access, or
ownership of completed experiments. This feature is account-specific draft
storage **within the single 'local' project**, not full multi-project tenancy.


## Durable local-model request obligations

Every reserved local-model request now has a fixed 30-second database
deadline. The separate worker reconciles expired `RESERVED` entries to
`UNKNOWN` without retrying the request, implying model success, or returning
reserved calls/tokens to the daily budget. Observed token usage is retained
when a late response provides an explicit count, but the request still remains
`UNKNOWN`.

The operator-only `GET /api/agent/model-budget` response includes
`calls_inflight` and `calls_unknown` for the current day. A configured model
that responds after its reservation deadline is replaced by the deterministic
fallback plan. A process crash may leave an ambiguous external model effect;
it cannot be treated as a certified result or silently retried. The system
does **not** reconcile the model provider's actual billing.

### Disposable role permissions

Use a dedicated, password-authenticated target cluster. Keep unrelated databases
off it and restrict access to maintenance databases in pg_hba.conf. Public
CONNECT grants on *other* databases are outside ClashBolt's per-run grants.
The runner gets at most two sessions, read access to inventory, and update
access to the two counters. Cluster setup requiring a client private key for
the administrator must separately allow the disposable password-authenticated
roles. The provisioner does not delegate the administrator's client key.

## Ungated native fixture measurement (trusted local research only)

After configuring separate native local PostgreSQL control and target clusters,
the fixed synthetic inventory fixture can be sampled **without the four
application checkpoint release gates**. Run this only on a trusted,
disposable machine with the target administrator credential in the
administrator's terminal, not inside an API process:

```powershell
python -m clashbolt.ungated --variant racy --trials 3
python -m clashbolt.ungated --variant locked --trials 3
```

The first command may report any mixture of PASS and FAIL depending on
natural concurrency timing; do not claim reproduction unless an actual
failure occurred. The fixed corrected variant should pass. The tool
creates and drops a fresh PostgreSQL database and restricted user for each
trial. It reports only per-client observations, the final state, wall time
and invariant result. It deliberately does **not** infer a global execution
order, issue a durable verified witness, perform general-target fuzzing,
or claim an independent vulnerability finding.

CI also runs a separate guarded test that force-stops the **ephemeral
control** PostgreSQL cluster after it acknowledges a run and reclaims
its worker lease after restart. That crash drill refuses execution outside
GitHub Actions or on a non-`clashbolt_test` database. It validates only
local primary recovery, not HA failover, replication, or external restore
SLAs. No Docker or network-supplied target is involved.


## Multiple isolated project workspaces (named accounts)

Schema revision **6** adds the `project_workspace` registry and projects'
independent PostgreSQL schemas. The original `local` project still uses
`public`, so existing named-account installations retain their local runs,
approval histories, and drafts. Every additional project is provisioned
**only from the trusted local CLI**; never accept project identifiers as
SQL schema names or arbitrary target URLs:

```sh
python -m clashbolt.accounts migrate
python -m clashbolt.accounts create-project scenario-a
python -m clashbolt.accounts grant-project alice.operator operator --project scenario-a
python -m clashbolt.accounts grant-project alice.reviewer reviewer --project scenario-a
python -m clashbolt.accounts revoke-project alice.operator --project scenario-a
```

Account names in the commands must already exist and have compatible global
role ceilings. No HTTP user signup or project creation is supported.
`GET /api/auth/projects` lists only the authenticated account's current
grants. `POST /api/auth/select-project` accepts a typed `project_id`,
requires a live grant, and stores only the selector in an HttpOnly cookie;
it does not grant authority. Every resource read/write, independent
review, agent plan, tool debit, and verification job uses the chosen
server-side schema. The React project selector clears old evidence and
reloads the target workspace. Workers in account mode rotate through
the trusted registry and process each project's durable run and
verification queues independently.

Deleting a member grant or disabling the account revokes their
previously issued capabilities in all affected project schemas.
Copied cookies and tokens cannot access another project's rows.
The global account role is the upper bound: reviewers cannot operate
and operators cannot approve their own rules.

**Boundary:** PostgreSQL schema segregation provides a query namespace,
not separate database clusters, Unix security identities, or a hard
isolation boundary against a compromised database administrator.
All projects currently use only the fixed, allowlisted synthetic
inventory target. Global resource reservation, independent project
backups, network segmentation and external-target instrumentation
still need separate security and recovery validation.
