# ClashBolt TITAN-20 implementation status

Source: **INTERLEAVE TITAN-20 Architecture** (3 November 2026), renamed ClashBolt.
Status for all 20 rows is **partially implemented**. The middle column records implemented scope and the right column records missing work. Verification applies only to the evidence described below.
All observed verdicts are limited to a shipped synthetic fixture. Partial is not complete.

| Track | Implemented scope | Remaining for complete specification |
| --- | --- | --- |
| B1 Durable Experiment Ledger | Atomic admission, version-CAS drafts, atomic migrations, private native archive and verified disposable-cluster restore drill | Multi-node failover, external recovery objectives and schema rollback drills |
| B2 Fenced Worker Claims | Generation-fenced run and verification claims, rejected stale receipts, worker heartbeats, 120 deterministic claim/expire/cancel/duplicate fault injections, one killed claimant subprocess/reclaim regression | Full worker/target process suspension and network-partition tests, database failover and target-effect readback |
| B3 Disposable Target Isolation | Connection-disabled creation from template0, per-run login with two sessions and column-limited writes, orphan cleanup, bounded child resources, hardened API/worker systemd templates | OS-user/process containment, deny-egress network policy, resource quotas and demonstrated teardown after unclean power loss |
| B4 Bounded Schedule Controller | Controlled two-client gate orders, watchdog | Instrumented general target, uncontrolled replay |
| B5 Independent Invariant Oracle | Independently reconstructed history, approved version bound to durable admission/commit, revocation withdraws verdicts | Identity-verified human reviewers, general rules and re-evaluation of real findings |
| B6 Minimal Witness Reproducer | Data-only hashed ZIP and clean fixture verifier, version-bound minimization, credential-isolated worker replay/reduction jobs | General-purpose minimization and externally proven cold-machine application reproduction |
| B7 Budgeted Admission and Recovery | Transactional quota/gate budget, model-call reservations with expiring durable obligations, native claim slots, oldest queue age and first-start P95 | Measured CPU/provider cost, multi-project fair scheduling and load-tested SLOs |
| A1 Invariant Workshop | Strict fixed-template rule candidates with source reference, rationale, hash-bound PASS/FAIL/UNKNOWN test vectors and reviewer gate | Model generation of new independently grounded rules and mutation suites |
| A2 Test Planner | Deterministic and validated local-model schedules, bounded typed plans, atomic local model request reservation and fallback | Novel target planning, end-to-end CPU/provider cost and held-out model evaluation |
| A3 Capability Tool Broker | Hashed short-lived scope-limited tokens, plan-history integrity check, oracle-first row locking against concurrent revocation, single-action audit and budgets | Project-specific identity, external fault testing of revocation races, and multitenant limits |
| A4 Evidence-Calibrated Abstention | Structured persisted missing-receipt, withdrawn-oracle, incomplete and conflict gaps; UNKNOWN never becomes a finding | Evidence-calibrated confidence and independently reviewed bounded probes |
| A5 Agent Decision Record | Typed plans, in-flight and model-call deadlines, UNKNOWN timeout reconciliation without budget refunds, hash-chained dispatch/receipt history, bound immutable proposals and feedback | Full model-context obligations, independent signatures, external crash-point matrix and context-corruption recovery |
| A6 Counterexample Replanner | Idempotent PostgreSQL feedback records and one bounded follow-up proposal based on a verified receipt, without automatic tool authority | Feedback across multiple unknown applications and held-out ablation |
| A7 Agent Evaluation Gate | Hashed synthetic receipts, descriptive paired bootstrap, and strict data-only held-out comparison intake with digest binding, split separation, full baseline matrix and cost caps; never auto-promoted | Genuine preregistered external corpus, independently authenticated adjudication and cost evidence, promotion/rollback decisions |
| P1 Experiment Composer | Named-account login, operator-only admission, approved oracle, account-specific CAS drafts and native Chromium end-to-end tests | Multi-project ownership and durable browser fault matrix |
| P2 Causal Run Timeline | Cursor-paged PostgreSQL events, SSE/snapshot reconciliation, monotonic browser version fencing and lost-SSE Chromium tests | Sustained disconnects, multi-worker browser chaos and long-run tests |
| P3 Witness Review Desk | JSON/ZIP evidence, independent checker, worker-queued native replay/reduction, version-fenced reports and revocation | Externally supported target reproduction and independent adjudication UI |
| P4 Approval Console | Named local viewer/operator/reviewer accounts, password hashing/lockout, PostgreSQL-backed per-direct-peer login throttling, role-bound signed sessions, revoked-account enforcement, selectable pending rules; legacy independent reviewer token | Multi-project isolation, SSO, administrative identity verification and cross-tenant tests |
| P5 Live Run Operations | Cancellation, SSE reconnect, per-account drafts, model uncertainty, worker and queue gauges, fixed operator-facing backlog diagnostics | Real CPU/provider cost, notification delivery, production alert tuning and load-tested UX |
| S1 Semantic Conflict Frontier | Deterministic receipt-ranked inventory conflict frontier with exploration penalty and operator-gated typed plans | Model-guided novel conflict discovery and independent equal-budget held-out superiority |

## Dependency-ordered next gates

1. Restrict target execution with tested OS identities, CPU/memory limits and deny-egress policy; validate untrusted target isolation before allowing any external applications.
2. Extend named single-project accounts into project-scoped roles and verified independent human review, with multi-tenant isolation tests.
3. Complete agent obligations and evidence provenance, general approved-rule DSL, real model-guided search and frozen external held-out comparisons.
4. Verify host-level systemd network denial, multi-node PostgreSQL failover/crash-point recovery, external independent reproduction and sustained load/security reviews before production certification.

## Claim boundaries

The optional local-model plan adapter is unpromoted; mocked adapter tests do not prove any installed model performs well. The built-in benchmark manifest is a **known training fixture**, not held-out AI evidence. The synthetic `FAIL` demonstrates only an intentional test-fixture bug under controlled application checkpoints. Repeating its digest in a fresh Python process is not ungated or real-world reproduction. A generation fence covers database **control-plane state**, not arbitrary external effects. Fixed child processes are not secure sandboxes. Named local role accounts are not external identity verification or multitenant authorization. A local operator token is not proof of independently authenticated reviewer identity. The semantic AI research hypothesis is **unverified**, and there is no exactly-once execution claim.


## Verified milestone notes

GitHub Actions exercises native PostgreSQL concurrency and replay, the built
React frontend, and Chromium account/transport scenarios. The held-out
evaluation protocol is a data-only intake, not an independently replicated
experiment. Model timeout recovery consumes reservation budgets conservatively
and explicitly reports uncertainty. Host-level isolation, multi-project
tenancy, real external held-out targets and independent study results remain
required before claiming complete PDF or production readiness.

## Adversarial security review (20 November 2026)

Scope: manual code review and targeted fail-closed regression tests against the
shipped local synthetic fixture, API, native target provisioning, worker, frontend,
and CI configuration. **No live production penetration test or external-target
attack was performed.** Passing synthetic tests cannot establish zero vulnerabilities.

Hardening committed directly to `main`:

- Native PostgreSQL target validation now rejects control/target port reuse even
  through different loopback aliases. It also rejects remote libpq `hostaddr`,
  connection-service redirection, and unsafe inherited connection settings.
- The fixed child runner now limits stdout *during* capture and stops a child
  that exceeds 100 KB, rather than buffering an unlimited response.
- The API now rejects malformed HTTP Host headers; bundled static deployments
  reject cross-origin browser write Origins and no longer grant CORS access to Vite ports.
- GitHub Actions now uses a contents-read-only token and prevents checkout from
  persisting Git credentials. Regression tests exercise these code boundaries.

Unresolved security/production gates: demonstrated independent host/process and
deny-egress containment; independently verified target/control cluster identity;
real multi-project authorization and tenant isolation; trusted external identity;
proxy-aware client identity and multi-node login throttling (current limit is direct-peer only);
dependency lockfiles, vulnerability scanning and software provenance; real
worker crash/partition/failover tests; and external independent security review.
The main branch is not currently protected. These issues remain open rather
than being represented as completed or solved by the new unit tests.

## Incremental authentication hardening (21 November 2026)

- B2 now includes a real POSIX claimant subprocess terminated after a durable
  claim. A replacement worker generation reclaims the run and rejects stale
  and duplicate receipts. The database lease is advanced for test speed; this
  does not demonstrate network partition, full worker interruption mid-effect,
  or multi-node failover.
- Account schema revision 3 adds a persisted PostgreSQL one-minute attempt
  budget, enforced atomically per direct loopback peer **before** PBKDF2 work.
  Excess requests return HTTP 429 with a fixed Retry-After hint.
- Tests cover concurrent reservation, rollover, and denial of further password
  verification work at the HTTP boundary. This mitigates local brute-force/CPU
  abuse but is not a proxy-aware Internet-facing identity or abuse-prevention
  service. Keep the existing loopback restriction in place.
- Upgrade: run `python -m clashbolt.accounts migrate` before starting the
  API in accounts mode. Its readiness check rejects incompatible schema versions.

## B3 native database isolation (21 November 2026)

The provisioner creates the database closed to connections and the role with
login disabled. It removes public database privileges before opening the
database. Only after schema setup commits does it enable the disposable login.
The runner DSN uses the actual connected endpoint and server-verification
settings. Administrator client keys, passwords, passfile, and startup options
are excluded. A runner can read inventory and update the two counters.

Verified locally with two disposable PostgreSQL 16.2 clusters:
`backend/tests/test_target_db.py` (11 passed). The regression uses two live
roles and checks cross-database denial, temporary and permanent table denial,
forbidden column updates, deletion, a third connection, and absence of admin
options from the runner DSN. Host isolation and external validation remain open.
