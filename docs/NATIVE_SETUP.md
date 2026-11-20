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


## 13. Named local accounts (optional single-project role mode)

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

A reviewer can select a pending proposal in the React console after signing in
without possessing an operator credential. An operator can then sign back in
to execute the separately approved proposal.

To revoke an account and invalidate its sessions immediately, run:

```powershell
python -m clashbolt.accounts disable experiment-operator
```

To reset a password and invalidate existing sessions:

```powershell
python -m clashbolt.accounts reset independent-reviewer
```

This is **single-project local account authorization**, not SSO,
multi-organization tenancy, production TLS termination, device trust, or a
verified two-person approval process. Do not make the API Internet-facing.
