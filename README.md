# ClashBolt

ClashBolt is a Python/FastAPI + React/TypeScript research lab for **application-level concurrency invariants**. The source specification is the 28-page *INTERLEAVE TITAN-20 Architecture* (3 November 2026), before the project was renamed ClashBolt.

**Working scope:** a bounded, locally hosted **synthetic inventory race**. The deliberately faulty variant accepts two orders for one available item under controlled overlap; the corrected variant rechecks stock. The fixed versioned oracle returns `PASS`, `FAIL` or `UNKNOWN`. This is a synthetic demonstration, **not** a finding against a real application.

## Implemented foundation

- **Python/FastAPI:** typed scratch experiments, PostgreSQL atomic admission, idempotency, versioned transitions, generation-fenced worker claims, bounded step usage, cancellation and SSE. Durable commands require the local operator token.
- **Worker and evidence:** fixed subprocess with a minimized environment; fresh memory fixture or isolated native PostgreSQL target database per run, bounded gates, independent reconstruction of reads/commits, version-bound oracle, and fresh-fixture replay. A generation-fenced target ledger supports cleanup after worker crashes. Revocation withdraws historic certifications without deleting evidence.
- **Witness reducer:** bounded application-gate deletion trials on clean fixtures, version-fenced persisted reduction reports, and a React review action. It does not claim arbitrary program minimization.
- **Agent boundary:** versioned demo-rule proposals, optional separate reviewer credential, revocable typed tool capabilities and audit, persisted plan receipts and timeouts-to-UNKNOWN; optional local-model schedule suggestions with deterministic fallback, and deterministic semantic/random/coverage comparison.
- **React/TypeScript:** experiment composer with target isolation selection, witness review and gate minimization, durable run monitor, cancellation, optional private operator unlock, and approval/capability console.
- **Evaluation:** repeatable synthetic strategy comparison with a hashed known-fixture manifest, equal gate budgets and per-trial receipts. It explicitly refuses AI policy promotion without held-out evidence.
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

For PostgreSQL durable mode: configure `CLASHBOLT_DATABASE_URL` and `CLASHBOLT_OPERATOR_TOKEN`. Run `python -m clashbolt.policy` to initialize/upgrade tables, start `python -m clashbolt.worker` in its own terminal, then the API and frontend. The rule must be approved before submitting a durable run. For per-run native PostgreSQL target databases, configure a **separate local PostgreSQL cluster** and `CLASHBOLT_TARGET_ADMIN_URL`. For read protection, enable `CLASHBOLT_ACCESS_MODE=private` and set a **different** `CLASHBOLT_REVIEWER_TOKEN`. See [native setup](docs/NATIVE_SETUP.md) for details. A durable run is never executed in an API background task.

## Tests and limitations

```bash
python -m ruff check .
python -m pytest -q
cd frontend && npm run build
```

Set `CLASHBOLT_TEST_DATABASE_URL` to a **disposable test-only PostgreSQL database** to run integration tests; test fixtures truncate their own tables. GitHub Actions prepares its own native PostgreSQL instance.

Export a witness from the UI, then run `python -m clashbolt.witness path/to/witness.json`. The verifier independently reconstructs reads, commits, state transitions and the oracle verdict, rejects inconsistent evidence even if its hash was recomputed, and replays controlled checkpoints in a fresh process. This does **not** establish uncontrolled reproduction.

**Security:** In standard local mode, durable mutations require `CLASHBOLT_OPERATOR_TOKEN` and reads are loopback-only. Optional `private` mode uses a time-limited HTTP-only session for reads and the token for writes. The API rejects direct non-loopback clients but can still be exposed unsafely through a reverse proxy. Project identity, multitenant RBAC, OS-level target isolation and external target support are not yet implemented. Bind the API to `127.0.0.1` only. This token is not production identity. The deterministic planner is **not** a validated LLM agent. No Docker, Temporal, Redis, Kafka or Kubernetes. Keep changes small, explicit and tested; commit directly on `main` with no branches or PRs.
