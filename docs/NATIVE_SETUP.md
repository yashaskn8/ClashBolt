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
python -m clashbolt.store
python -m clashbolt.worker
```

Run `python -m clashbolt.policy` if you need the optional oracle-review and
scoped-tool tables. These routes do not auto-create schema; startup will fail
closed until explicitly migrated.

In a second terminal sharing the environment variables:

```powershell
uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000
```

The API accepts experiments into the PostgreSQL queue. The **separate worker**
process claims and executes each accepted synthetic run. API requests do not
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

The verifier accepts only shipped synthetic fixture contracts and compares
evidence hashes after running the same application gate order on fresh state.
This does **not** prove a real-world or uncontrolled concurrency bug.

## 4. What the operator token protects

`CLASHBOLT_OPERATOR_TOKEN` must be set before approving a candidate oracle,
issuing/revoking scoped capabilities, inspecting tool audit, or evaluating
planner baselines. Send it in the `X-Operator-Token` header from a trusted
local client. Do not commit it or expose it in a URL.

The token is a single-machine operator control, not identity verification,
multitenant RBAC, an independent human review system, or production authorization.
The planner only proposes deterministic plans; it cannot mint tokens or change
the checker. Tool execution accepts solely the built-in fixture and approved
variant/schedule pair. Revocation prevents future calls but cannot undo work
already started.
