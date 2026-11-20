# ClashBolt implementation status

Source: **INTERLEAVE TITAN-20 Architecture** (3 November 2026), renamed ClashBolt. The 20 features are planned; they are not all implemented.

## Stage 1 — synthetic vertical slice

| Area | Current evidence | Remaining boundary |
| --- | --- | --- |
| B3 Disposable Target Isolation | New in-memory inventory fixture per run/replay; no external endpoints | Isolated process and PostgreSQL target database, deny egress, teardown proof |
| B5 Independent Invariant Oracle | Immutable SHA256-bound demo rule; PASS/FAIL/UNKNOWN; evidence | Authenticated human review, approval storage, checker isolation and withdrawal |
| B4 Bounded Schedule Controller | Two asyncio clients, explicit application checkpoints, recorded gate order, watchdog | Controlled target process and independent/uncontrolled reproduction |
| A3 Capability Tool Broker | Strict allowlist, typed API, no URL/script/tool input; local-only mode | Issued/revocable scoped capabilities, quota ledger and audit persistence |
| P1 Experiment Composer | React/TypeScript form driven by local catalog; validation enforced by FastAPI | Authenticated project ownership, durable experiment admission |
| P3 Witness Review Desk | Fixture + oracle digest + ordered evidence + clean replay comparison | Independent witness export, external replay and reviewer sign-off |

## Next implementation stages

1. **Durable control plane** — PostgreSQL experiment/transition/job records (B1), worker generation fencing (B2), cost admission (B7), witness export (B6), timelines and operations (P2/P5).
2. **Agent** — immutable approval UI (P4), proposals (A1), broker-issued capabilities (A3), bounded planner (A2), evidence-calibrated abstention (A4), durable decisions (A5).
3. **Research** — replanning (A6), frozen holdouts and promotion gate (A7), semantic conflict frontier (S1), equal-budget baselines.
4. **Hardening** — failure injection, multi-run isolation, security/authorization, clean-machine reproduction, native process deployment and load tests.

## Claims and limitations

- `FAIL` proves only that the deliberately faulty **synthetic fixture** violates the shipped demo invariant under its recorded gate order.
- The fixed demo rule is not a human-approved rule for an outside application.
- A byte-identical local replay is not independent, ungated, or production reproduction.
- API state is process-local and disappears on restart; PostgreSQL and durable jobs are intentionally future work.
- The API has no authentication. Bind to loopback only and **never expose it on a public network**.
- No Docker, Temporal, Redis, Kafka, Kubernetes, arbitrary network targets, or automatic model authority.
