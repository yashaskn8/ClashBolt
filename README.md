# ClashBolt

ClashBolt is a Python/FastAPI + React/TypeScript research lab for **application-level concurrency invariants**. The source specification is the 28-page *INTERLEAVE TITAN-20 Architecture* (3 November 2026), before the project was renamed ClashBolt.

**Working scope:** a bounded, locally hosted **synthetic inventory race**. The deliberately faulty variant accepts two orders for one available item under controlled overlap; the corrected variant rechecks stock. The fixed versioned oracle returns `PASS`, `FAIL` or `UNKNOWN`. This is a synthetic demonstration, **not** a finding against a real application.

## Implemented foundation

- **Python/FastAPI:** typed scratch experiments, PostgreSQL atomic admission, idempotency, event transitions, lease-generation fencing, queue quota, cancellation and SSE.
- **Worker and evidence:** short-lived fixed synthetic subprocess, fresh fixture on each run, bounded schedules, parent-side oracle and digest verification, exported evidence, separate-process verifier CLI.
- **Agent boundary:** immutable demo-rule proposals, operator-token approval, scope-limited revocable capabilities, tool audit, persisted typed plans and deterministic semantic/random/coverage comparison.
- **React/TypeScript:** experiment composer, witness review, PostgreSQL run monitor and cancellation, local approval and capability console.
- **CI:** native PostgreSQL integration tests, Ruff, TypeScript and Vite build; no Docker.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the evidence-based 20-feature status and gaps. This is **not** the completed 20-feature AI discovery platform or a production-ready multi-tenant system.

## Native setup (no Docker)

See [docs/NATIVE_SETUP.md](docs/NATIVE_SETUP.md) for PostgreSQL setup, PowerShell, the separate worker process, operator secret and CLI verification.

In-memory scratch mode:

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Try **Racy / Overlap** for a controlled failure, then **Corrected / Overlap** for a pass.

For PostgreSQL durable mode: set `CLASHBOLT_DATABASE_URL`, run `python -m clashbolt.policy` to initialize all tables, start `python -m clashbolt.worker` in its own terminal, then run the API and frontend. A DB-backed run is not executed inside an API background task.

## Tests and limitations

```bash
python -m ruff check .
python -m pytest -q
cd frontend && npm run build
```

Set `CLASHBOLT_TEST_DATABASE_URL` to a **disposable test-only PostgreSQL database** to run integration tests; test fixtures truncate their own tables. GitHub Actions prepares its own native PostgreSQL instance.

Export a witness from the UI, then run `python -m clashbolt.witness path/to/witness.json`. The verifier rejects changed evidence and replays the declared controlled checkpoints in a fresh Python process; this does **not** establish uncontrolled reproduction.

**Security:** API authentication, multitenant RBAC, OS-level target isolation and external target support are not yet implemented. Bind the API to `127.0.0.1` only. Operator endpoints require `CLASHBOLT_OPERATOR_TOKEN`, but this is not production identity. The deterministic planner is **not** a validated LLM agent. No Docker, Temporal, Redis, Kafka or Kubernetes. Keep changes small, explicit and tested; commit directly on `main` with no branches or PRs.
