# ClashBolt TITAN-20 implementation status

Source: **INTERLEAVE TITAN-20 Architecture** (3 November 2026), renamed ClashBolt.
All observed verdicts are limited to a shipped synthetic fixture. Partial is not complete.

| Track | Implemented scope | Remaining for complete specification |
| --- | --- | --- |
| B1 Durable Experiment Ledger | Atomic PostgreSQL admission, transitions and idempotency | Crash/failover and migration matrix |
| B2 Fenced Worker Claims | Generation fencing, worker claims, stale-write rejection | Target-effect reconciliation and pause/resume faults |
| B3 Disposable Target Isolation | Fresh memory fixture in short-lived fixed subprocess | Isolated target database, OS account and deny-egress enforcement |
| B4 Bounded Schedule Controller | Controlled two-client gate orders, watchdog | Instrumented general target, uncontrolled replay |
| B5 Independent Invariant Oracle | Independently reconstructed history, approved version bound to durable admission/commit, revocation withdraws verdicts | Identity-verified human reviewers, general rules and re-evaluation of real findings |
| B6 Minimal Witness Reproducer | JSON evidence, digest, independent clean-process verifier | Automated minimization and external reproducible bundles |
| B7 Budgeted Admission and Recovery | Active quota, reserved gate budget, consumed-gate reconciliation, cancellation | CPU/model billing, target slots, fair admission benchmarks and backpressure |
| A1 Invariant Workshop | Fixed rule proposal/review | Constrained agent-generated candidate DSL |
| A2 Test Planner | Typed deterministic schedules | Real model proposals and bound total cost |
| A3 Capability Tool Broker | Hashed short-lived scope-limited tokens, revoke, audit | Production identity and fault matrix |
| A4 Evidence-Calibrated Abstention | UNKNOWN, deterministic abstention, persisted missing-receipt evidence gap | Model-specific evidence calibration and bounded diagnostic probes |
| A5 Agent Decision Record | Typed plan, in-flight deadline, final receipt and expired-action recovery to UNKNOWN | Complete obligations, schema/version migration and crash-point tests |
| A6 Counterexample Replanner | Deterministic finite follow-up | Persist feedback and test adaptive policy |
| A7 Agent Evaluation Gate | Equal-budget local baseline comparison | Frozen held-out suites, promotion and rollback |
| P1 Experiment Composer | React typed form, approved oracle selection, operator-token-gated durable admission | Authenticated project ownership and durable drafts |
| P2 Causal Run Timeline | Versioned events, SSE and snapshot reconciliation | Omission/reordering and cross-worker causal tests |
| P3 Witness Review Desk | Evidence viewer, export, independently validated history, fresh-process replay and authoritative revocation display | Witness reduction, outside-machine reproduction and review decisions |
| P4 Approval Console | React operator review and capability actions | Identity-backed roles and segregation of duties |
| P5 Live Run Operations | Operator-token cancellation, status/reconnect, transition history and consumed gate budget | CPU/model budgets and full recovery UX |
| S1 Semantic Conflict Frontier | Fixed inventory conflict edge, deterministic comparator | True model-guided search and held-out research evidence |

## Dependency-ordered next gates

1. Isolated target PostgreSQL databases and restricted OS identities with enforced deny-egress; authenticated reviewer identity and general oracle versions.
2. Persistence recovery, worker cancellation/lease fault injection, fair-budget accounting and independent witness reduction.
3. Restricted agent adapter, obligation/receipt persistence, evaluation on frozen held-out targets and human policy promotion.
4. Project authentication and role separation, audited operations, real end-to-end browser tests and native deployment hardening.

## Claim boundaries

The synthetic `FAIL` demonstrates only an intentional test-fixture bug under controlled application checkpoints. Repeating its digest in a fresh Python process is not ungated or real-world reproduction. A generation fence covers database **control-plane state**, not arbitrary external effects. Fixed child processes are not secure sandboxes. A local operator token is not proof of independently authenticated reviewer identity. The semantic AI research hypothesis is **unverified**, and there is no exactly-once execution claim.
