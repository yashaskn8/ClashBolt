# ClashBolt

ClashBolt is an AI Concurrency Intelligence research project for reproducible **application-level business-invariant failures** in concurrent APIs. Its design authority is the 28-page **INTERLEAVE TITAN-20 Architecture** (3 November 2026), created before the project was renamed.

## First runnable slice: a synthetic inventory clash

A **local-only** two-client fixture shows why checking stock before concurrent commits can accept more orders than inventory allows. The `racy` variant demonstrates the problem; the `locked` variant revalidates current stock before accepting an order. Explicit application checkpoints provide two repeatable gate orders. A fixed versioned checker returns `PASS`, `FAIL`, or `UNKNOWN`; the witness includes the fixture hash, oracle version, gate history, final state, and evidence digest. A second run on fresh state can confirm the same synthetic witness.

This is **not** a general concurrency scanner, real-target finding, independent reproduction, production-ready service, or implementation of all 20 planned features. The demo rule is fixed in code and not a human-approved production oracle.

### Native local setup (Python 3.11+ and Node 20+; no Docker)

Backend (in a terminal from repository root):

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000
```

Frontend (another terminal):

```bash
cd frontend
npm install
npm run dev
```

Visit `http://127.0.0.1:5173`. Choose **Racy + Overlap** for a synthetic `FAIL`, **Racy + Serial** or **Corrected** for `PASS`. Click **Replay on a clean fixture** to compare deterministic evidence hashes.

### API example

```bash
curl http://127.0.0.1:8000/api/catalog
curl -X POST http://127.0.0.1:8000/api/experiments -H "Content-Type: application/json" -d '{"variant":"racy","schedule":"overlap"}'
```

`POST /api/experiments` returns a `run_id` and evidence. `GET /api/experiments/{run_id}` retrieves it; `POST /api/experiments/{run_id}/replay` runs the fixture again and reports `REPRODUCED` only if complete evidence hashes match. API input rejects custom URLs, scripts, and unsupported targets.

### Tests

```bash
python -m pytest -q
python -m ruff check .
cd frontend && npm run build
```

**Safety:** The backend is unauthenticated and has ephemeral in-memory state. Run it on loopback only; never publish it to the internet. It does not execute code from external systems or connect to target services. For feature-by-feature implementation status and limitations, see [docs/ROADMAP.md](docs/ROADMAP.md).

## Architecture and working rules

- Python/FastAPI backend; PostgreSQL is planned for durable control state.
- React/TypeScript/Vite frontend.
- No Docker, Temporal, Redis, Kafka, or Kubernetes.
- Keep code small and readable, verify changes, and commit directly on `main` without branches or PRs.
- Preserve the distinction between controlled synthetic behavior and independently reproduced real findings.
