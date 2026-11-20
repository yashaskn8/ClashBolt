# ClashBolt TITAN-20 implementation status

Source: **INTERLEAVE TITAN-20 Architecture** (3 November 2026), renamed ClashBolt.
All observed verdicts are limited to a shipped synthetic fixture. Partial is not complete.

| Track | Implemented scope | Remaining for complete specification |
| --- | --- | --- |
| B1 Durable Experiment Ledger | Atomic admission and events, idempotency, version-CAS drafts, serialized atomic migrations, verified private native backup archive | Restore drills, multi-node failover, recovery and schema rollback tests |
| B2 Fenced Worker Claims | Generation-fenced run and verification claims, rejected stale receipts, worker heartbeats, 120 deterministic claim/expire/cancel/duplicate fault injections | Actual process suspension/crash and network-partition tests, database failover and target-effect readback |
| B3 Disposable Target Isolation | Fresh per-run native PostgreSQL database and role, orphan cleanup, bounded child resources, hardened API/worker systemd templates | OS-user/process containment, deny-egress network policy, resource quotas and demonstrated teardown after unclean power loss |
| B4 Bounded Schedule Controller | Controlled two-client gate orders, watchdog | Instrumented general target, uncontrolled replay |
| B5 Independent Invariant Oracle | Independently reconstructed history, approved version bound to durable admission/commit, revocation withdraws verdicts | Identity-verified human reviewers, general rules and re-evaluation of real findings |
| B6 Minimal Witness Reproducer | Data-only hashed ZIP and clean fixture verifier, version-bound minimization, credential-isolated worker replay/reduction jobs | General-purpose minimization and externally proven cold-machine application reproduction |
| B7 Budgeted Admission and Recovery | Transactional quota and gate accounting, cancellation, atomic daily model budget, serialized native target-claim slot cap with memory-job bypass | Actual host resource cost, multi-project fair scheduling, load stress and end-to-end latency SLOs |
| A1 Invariant Workshop | Strict fixed-template rule candidates with source reference, rationale, hash-bound PASS/FAIL/UNKNOWN test vectors and reviewer gate | Model generation of new independently grounded rules and mutation suites |
| A2 Test Planner | Deterministic and validated local-model schedules, bounded typed plans, atomic local model request reservation and fallback | Novel target planning, end-to-end CPU/provider cost and held-out model evaluation |
| A3 Capability Tool Broker | Hashed short-lived scope-limited tokens, revocation, plan-bound scope, audit and bounded tool calls; private mode accepts scoped dispatch without operator cookies | Project-specific identity, lease/revocation fault matrix and multitenant limits |
| A4 Evidence-Calibrated Abstention | Structured persisted missing-receipt, withdrawn-oracle, incomplete and conflict gaps; UNKNOWN never becomes a finding | Evidence-calibrated confidence and independently reviewed bounded probes |
| A5 Agent Decision Record | Typed plans, in-flight deadlines, verified receipts, expired-action recovery, append-only SHA-256 chained history, immutable proposal binding before dispatch/receipt, and feedback | Model-context obligations, independent key-backed signing, crash-point matrix and context-corruption recovery |
| A6 Counterexample Replanner | Idempotent PostgreSQL feedback records and one bounded follow-up proposal based on a verified receipt, without automatic tool authority | Feedback across multiple unknown applications and held-out ablation |
| A7 Agent Evaluation Gate | Hashed synthetic manifest, equal-gate receipts, paired descriptive bootstrap interval, explicitly ineligible promotion | Frozen *external held-out* suites, independent target replication, equal total cost, authenticated promotion and rollback |
| P1 Experiment Composer | Named-account login, operator-only admission, approved oracle, account-specific CAS drafts and native Chromium end-to-end tests | Multi-project ownership and durable browser fault matrix |
| P2 Causal Run Timeline | Versioned PostgreSQL events, cursor pagination, Last-Event-ID resume, SSE hints and snapshot reconciliation | Browser fault injection for loss, reordering and long-running streams |
| P3 Witness Review Desk | JSON/ZIP evidence, independent checker, worker-queued native replay/reduction, version-fenced reports and revocation | Externally supported target reproduction and independent adjudication UI |
| P4 Approval Console | Named local viewer/operator/reviewer accounts, password hashing/lockout, role-bound signed sessions, revoked-account enforcement, selectable pending rules; legacy independent reviewer token | Multi-project isolation, SSO, administrative identity verification and cross-tenant tests |
| P5 Live Run Operations | Cancellation, SSE reconnect, paged events, saved drafts, target cleanup, gate/model budgets and operator-only worker/queue health | CPU/provider cost, alerts, operator runbooks and load-tested multiuser UX |
| S1 Semantic Conflict Frontier | Deterministic receipt-ranked inventory conflict frontier with exploration penalty and operator-gated typed plans | Model-guided novel conflict discovery and independent equal-budget held-out superiority |

## Dependency-ordered next gates

1. Restrict target execution with tested OS identities, CPU/memory limits and deny-egress policy; validate untrusted target isolation before allowing any external applications.
2. Extend named single-project accounts into project-scoped roles and verified independent human review, with multi-tenant isolation tests.
3. Complete agent obligations and evidence provenance, general approved-rule DSL, real model-guided search and frozen external held-out comparisons.
4. Verify host-level systemd network denial, backup restore into disposable PostgreSQL, multi-node failover/crash-point recovery, independent reproduction and sustained load/security reviews before production certification.

## Claim boundaries

The optional local-model plan adapter is unpromoted; mocked adapter tests do not prove any installed model performs well. The built-in benchmark manifest is a **known training fixture**, not held-out AI evidence. The synthetic `FAIL` demonstrates only an intentional test-fixture bug under controlled application checkpoints. Repeating its digest in a fresh Python process is not ungated or real-world reproduction. A generation fence covers database **control-plane state**, not arbitrary external effects. Fixed child processes are not secure sandboxes. Named local role accounts are not external identity verification or multitenant authorization. A local operator token is not proof of independently authenticated reviewer identity. The semantic AI research hypothesis is **unverified**, and there is no exactly-once execution claim.
