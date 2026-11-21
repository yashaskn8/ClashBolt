# ClashBolt TITAN-20 implementation status

Source: **INTERLEAVE TITAN-20 Architecture** (3 November 2026), renamed ClashBolt.
Status for all 20 rows is **partially implemented**. The middle column records implemented scope and the right column records missing work. Verification applies only to the evidence described below.
All observed verdicts are limited to a shipped synthetic fixture. Partial is not complete.

| Track | Implemented scope | Remaining for complete specification |
| --- | --- | --- |
| B1 Durable Experiment Ledger | Atomic admission, version-CAS drafts, atomic migrations, private native archive, disposable restore drill, CI primary-crash restart check, and injected migration DDL/DML rollback test | Multi-node failover, external recovery objectives and production upgrade/downgrade rollback validation |
| B2 Fenced Worker Claims | Generation-fenced run and verification claims, rejected stale receipts, worker heartbeats, 120 deterministic fault injections, live POSIX claimant suspension and SIGKILL native-run crash-point recovery | Network partitions, PostgreSQL failover, arbitrary external target-effect readback and broader mid-effect crash validation |
| B3 Disposable Target Isolation | Connection-disabled creation from template0, per-run login with two sessions and column-limited writes, generation-bound orphan cleanup, per-request systemd target, verifier SIGKILL and two native worker SIGKILL target-stage recovery tests | Independent deployment validation of UID, egress and resource enforcement; power-loss, multi-host and wider crash-point recovery drills |
| B4 Bounded Schedule Controller | Controlled two-client gate orders, watchdog, independent fixed native two-client ungated sampler with fresh disposable target per trial | Instrumented general targets and independently reproduced uncontrolled application failures |
| B5 Independent Invariant Oracle | Independently reconstructed history, approved version bound to durable admission/commit, revocation withdraws verdicts | Identity-verified human reviewers, general rules and re-evaluation of real findings |
| B6 Minimal Witness Reproducer | Data-only hashed ZIP and clean fixture verifier, version-bound minimization, credential-isolated worker replay/reduction jobs | General-purpose minimization and externally proven cold-machine application reproduction |
| B7 Budgeted Admission and Recovery | Transactional quota/gate budget, model-call reservations, atomic global two-native-target cap, transactional per-project native cap shared by run and verification claims, queue age/start P95, and bounded three-project worker drain | Sustained external/multi-project fair-load benchmarks, measured CPU/provider cost, retry storms and host-wide resource benchmarks |
| A1 Invariant Workshop | Strict fixed-template rule candidates with source reference, rationale, hash-bound PASS/FAIL/UNKNOWN test vectors and reviewer gate | Model generation of new independently grounded rules and mutation suites |
| A2 Test Planner | Deterministic and validated local-model schedules, bounded typed plans, atomic local model request reservation and fallback | Novel target planning, end-to-end CPU/provider cost and held-out model evaluation |
| A3 Capability Tool Broker | Hashed short-lived tokens, bounded typed plans, oracle-first revocation fencing, account+local-project issuer binding, live grant checks, permanent token revocation on role/credential change, race tests and single-action audit | General project-specific capability partitioning, external fault testing, and multitenant limits |
| A4 Evidence-Calibrated Abstention | Structured persisted missing-receipt, withdrawn-oracle, incomplete and conflict gaps; UNKNOWN never becomes a finding | Evidence-calibrated confidence and independently reviewed bounded probes |
| A5 Agent Decision Record | Typed plans, bounded model/tool deadlines, UNKNOWN recovery without budget refunds, hash-chained history, bounded worker-integrity sweeps with durable quarantine, versioned question/request/receipt obligations in PostgreSQL, and child-crash recovery tests | General multi-tool/schema migration semantics, independently authenticated signatures, external crash-point matrix and recovery validation |
| A6 Counterexample Replanner | Idempotent PostgreSQL feedback records and one bounded follow-up proposal based on a verified receipt, without automatic tool authority | Feedback across multiple unknown applications and held-out ablation |
| A7 Agent Evaluation Gate | Hashed synthetic receipts, descriptive paired bootstrap, and strict data-only held-out comparison intake with digest binding, split separation, full baseline matrix and cost caps; never auto-promoted | Genuine preregistered external corpus, independently authenticated adjudication and cost evidence, promotion/rollback decisions |
| P1 Experiment Composer | Named accounts, approved-oracle admission, per-account/per-project versioned drafts, React project selector, isolated Chromium project-switching regression, read-only project quota/approval/resource preview | Real general-target contracts, measured CPU/provider cost estimates and wider browser fault matrix |
| P2 Causal Run Timeline | Cursor-paged PostgreSQL events, SSE/snapshot reconciliation, monotonic browser version fencing and lost-SSE Chromium tests | Sustained disconnects, multi-worker browser chaos and long-run tests |
| P3 Witness Review Desk | JSON evidence, deterministic ZIP bundle, immutable run-version/export SHA256 journal, independent checker, worker-queued replay/reduction, version-fenced reports and revocation, named reviewer-only immutable synthetic evidence decisions and React review desk | Externally supported application reproduction, externally verified reviewer identity and portable independent adjudication |
| P4 Approval Console | Named local roles, signed sessions, audited project grants, separate PostgreSQL resource schemas, transaction-locked authorization, scoped broker tokens, reviewer approval, browser cross-project checks and live role sync | General external targets, independent identity/SSO, hard security boundaries between database administrators and tenants |
| P5 Live Run Operations | Cancellation, SSE reconnect, per-project status and run projections, model uncertainty, worker and queue gauges, quarantine diagnostics | Real CPU/provider cost, notification delivery, production alert tuning and load-tested UX |
| S1 Semantic Conflict Frontier | Deterministic receipt-ranked inventory conflict frontier with exploration penalty and operator-gated typed plans | Model-guided novel conflict discovery and independent equal-budget held-out superiority |

## P3 deterministic artifact export audit (21 November 2026)

Control schema **revision 8** adds an append-only, project-scoped
`witness_export` journal. `POST /api/runs/{run_id}/exports` requires operator
authority, independently validates the certified run and creates one idempotent
artifact digest for that exact run version and evidence hash. `GET` on the
corresponding export collection returns its history and recalculated
current/superseded state. Revocation invalidates current export authority,
but never deletes historical records.

The ZIP bundle now has fixed entry metadata and stores small data-only
members without compression so independent construction has stable bytes.
The existing `GET /api/runs/{run_id}/bundle` stays **read-only** and sends a
SHA256 response header. The browser's operator export action first records
the immutable digest, downloads the ZIP, and compares **the actual downloaded
bytes** against both the journal digest and the response header before
presenting the file. Unit, PostgreSQL and Chromium tests cover deterministic
bytes, idempotency, immutable audit, version withdrawal and browser export.
A direct read-only GET may still deliver an unaudited historical download;
only the operator export action records the audit.

The bundle remains a **synthetic-only data archive**, not a self-contained
external-application reproducer. This control-plane journal is neither
independently signed nor immutable against a privileged database operator;
real target credentials and external reproduction are still out of scope.

## P3 independently role-reviewed synthetic witness (21 November 2026)

Control schema **revision 7** adds an append-only, project-scoped
`witness_review` audit. A named account with the **reviewer** role may record
one immutable SUPPORTED or WITHHELD decision for a specific failed run
version and evidence SHA256. An independently queued worker `REPLAY` job
must be COMPLETE and have a matching synthetic-only replay report before
SUPPORTED is permitted. The server derives the eligible replay from its
own database; a client cannot substitute an arbitrary job or replay hash.
Repeat requests with identical content are idempotent. Operators, viewers,
stale run versions, changed evidence and foreign projects are denied.
Concurrent decisions on one run serialize under its PostgreSQL row lock.

GET returns the historical audit with a derived `current` flag. Revocation
withdraws the run's current verdict and marks existing reviews superseded
without deleting the audit. The React review desk exposes replay readiness,
reviewer-only explicit reason/decision controls and historical outcomes.
Browser and PostgreSQL tests cover this fixed synthetic workflow.

**No real-application adjudication is claimed.** Named local reviewer accounts
are not independently verified external human identity or professional
signatures. Hashes and PostgreSQL triggers are not protection from privileged
database administrators. The supported status explicitly means only that
the *shipped deterministic synthetic fixture* was replayed by the worker.
It is not a publication gate for a reported vulnerability in an external
application. External reproduction and independent reviewers remain open.

## A5 versioned agent obligation lifecycle (21 November 2026)

Policy schema **revision 11** adds `agent_obligation`: one durable,
versioned question per synthetic investigation plan with an explicitly
recorded model identifier and fixed tool schema version. Each new proposed
plan writes the question and its hash-chained PROPOSED event atomically.
The capability broker creates a unique tool request ID in the same
transaction as the one-call debit, IN_FLIGHT state transition and
DISPATCHED event. Receipt acceptance binds a unique receipt ID to that
request; timeout, review revocation or integrity quarantine never
manufactures a successful receipt and instead records UNKNOWN.

Every attempted authorization rechecks the persisted question, tool
request and version against immutable agent history. Modified or legacy
context without an authenticated history binding cannot gain tool
authority. A fresh process can read the outstanding question and
reconcile expired dispatches without retrying the ambiguous target
effect. The React agent console displays its context version,
fixed tool schema, model label, durable state and receipt presence.

**Upgrade:** run the existing `python -m clashbolt.policy` or
`python -m clashbolt.accounts migrate` command before starting updated
API and worker processes. Pre-revision-11 plans lack the new context
binding and must be treated as unconfirmed, rather than silently gaining
new authority. The safety boundary still protects only this fixed
synthetic tool and PostgreSQL control-plane records. Hash chaining is not
independently authenticated signing; a privileged database administrator
can rewrite data and signatures. Real external application crash points,
model schema evolution, multiple outstanding dependent tools and
production restoration are **not** validated by these tests.

## P1 non-reserving run preview (21 November 2026)

The experiment composer now calls an authorized `POST /api/runs/preview`
endpoint before submitting a durable synthetic experiment. Its project-scoped
PostgreSQL read checks the current approved-oracle binding and active/open-run
quota, validates that a configured native target endpoint is distinct from
the control endpoint, and reports the bounded checkpoint budget. React shows
the current known blockers and whether a disposable native database would be
requested. It refreshes when the selected run state or project changes.

**The preview is advisory, not an admission or capacity reservation.**
The actual run admission rechecks approval and quota transactionally. Native
slot claims are a distinct worker-time constraint; host CPU use, external
provider cost, execution duration and independent sandbox enforcement are
**not measured or promised** by the preview. A `preflight_ok` snapshot
cannot guarantee that a future submission or worker claim will succeed.
Regression coverage checks authorization, no quota consumption, exhaustion,
native/control alias rejection and differing quotas across two projects.
This remains partial P1 implementation under the source PDF.

## B7 host-wide native target admission (21 November 2026)

Under named-account project mode, all durable native target **run claims and
verification claims** now acquire one PostgreSQL transaction-scoped advisory
lock before any project quota lock. Within that transaction the worker
counts unexpired RUNNING/CANCELLING native experiment leases and unexpired
RUNNING verification leases across every trusted project schema, then
admits only when fewer than **two** host-wide slots are occupied. Memory
runs remain claimable even when the native host is full. This closes the
previous per-project-only capacity hole when several independent worker
processes claim different workspaces concurrently.

The integration regression races claims across three real schemas, checks
the two-slot invariant, blocks verification until a lease expires, and
checks that a waiting memory run is not starved by native capacity. This
is conservative admission based on the coordinator's current lease state,
not a cgroup resource controller: already executing external effects,
ambiguous expired leases, administrator bypass, multiple separate control
clusters, and host-level recovery behavior still need independent tests.

## B7 project-native admission and bounded fair-load regression (21 November 2026)

All active native run claims and worker-queued verification claims now share
each trusted project's existing `max_native_active` quota. Both admission
paths lock the same local `project_quota` row. Under named-account project
mode, they first acquire the existing host-wide advisory lock so a native
claim cannot exceed either the host or project limit. Expired leases do not
reserve capacity; a stale receipt is still rejected independently. Memory
execution remains eligible when native capacity is full.

The PostgreSQL regression races separate native and verification claimers
against a one-slot project, tests both directions of capacity release after
lease expiry, and proves another project can claim its own slot without
borrowing the exhausted project's capacity. A separate **bounded 30-run,
three-project** trial admits overlapping backlogs through real PostgreSQL
connections, rotates project priority across worker cycles, runs the actual
fixed-fixture worker entrypoint and checks all ten receipts per project. It
does not establish general production fairness, provider budget accounting,
CPU cost, network partition tolerance or durable throughput objectives.

## P4 multi-project storage and transaction authorization (21 November 2026)

Account schema **6** adds the trusted `project_workspace` registry, a
per-account/per-project draft key, and audited independent project grants.
New projects are created by the trusted local CLI with transactionally
provisioned PostgreSQL resource schemas; the original `local` project
continues using `public` for backward compatibility. Policy schema **10**
provisions issuer binding fields in every project. API request dispatch
looks up the stored schema; neither a cookie nor model output can choose
an arbitrary schema. Reviewer approval, experiment admission, agent plans,
capability issuance, verification jobs, run history and metrics resolve
only inside the selected project.

A typed `/api/auth/select-project` action updates an HttpOnly project
selector after checking current membership; the React workspace resets
stale evidence on switch. The native worker rotates its starting project,
follows the same server-side registry, and drains each project's bounded
durable queues. Chromium exercises reviewer/operator project switching,
draft recovery, private run history and the actual tenant worker.

After a middleware permission check, each user-scoped database connection
**rechecks and row-locks** the live account and grant for the transaction.
Concurrent project revocation thus serializes with writes, rather than
permitting a stale middleware decision to create new resources. Per-account
draft read/write transactions enforce the same boundary. The one capability
tool endpoint does not need a browser session but checks its issuing account
and exact project independently at the broker, with permanent revocation
on role/credential changes.

**Scope boundary:** PostgreSQL schema namespaces and application locking
do not protect against a compromised database superuser, do not supply
independent per-tenant OS sandboxes or target behavior, and do not certify
independent external real-application reproduction. The worker rotates
projects but measured global host CPU/memory budgets, sustained fair
load tests and multi-node failover are still outstanding. The 20-track
PDF is **not complete** merely because this release gate passes.

## Dependency-ordered next gates

1. Restrict target execution with tested OS identities, CPU/memory limits and deny-egress policy; validate untrusted target isolation before allowing any external applications.
2. Harden existing project-scoped roles with independently verified reviewer identity, separate database/host tenant credentials, cross-project resource quota limits, and external target authorization tests.
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

## B3 target execution boundary (21 November 2026)

Implemented in `target_service.py`, the worker, witness verifier, reducer and
`deploy/systemd/clashbolt-target*`. The installed worker uses socket activation
with one dynamic user and cgroup per connection. Only fixed schedules and
order-preserving subsets for reductions are accepted. The target can request
no executable, host, role override or connection option. The private root
exposes the runtime and designated target PostgreSQL socket. IP sockets are
disabled. The service has no control or target-administrator credential.
The original proc-less root restriction remains mandatory and is checked
by a negative fixture preflight. An attempted combination of additional
kernel/systemd protection directives exposed `/proc/1/environ` on the
disposable runner, so it was rolled back rather than weakening the probe.
The original syscall, namespace, privilege and cgroup controls remain.

Evidence: `test_target_service.py` covers the actual stdin/stdout handler,
input bounds, rejection paths and absent-socket failure. The distinct host
gate in `ci/check-target-service.sh` runs `test_target_host.py` and an
ExecStartPre confinement probe against the installed units. Local protocol
tests cannot verify host enforcement. Deployment-host review and power-loss
validation remain blocked pending independent infrastructure. Per-target
limits exclude the separate PostgreSQL server. External targets stay disabled.


## B3 native host acceptance and dynamic UID correction (21 November 2026)

The first native Linux systemd host drill exposed shared dynamic UIDs across
simultaneous socket-activated template instances. The fixture service now
declares `User=cb-%i` with `DynamicUser=yes`, which assigns a distinct
per-connection identity for the existing Unix socket. Its acceptance test
checks actual service MainPIDs (not short-lived startup helper PIDs), distinct
non-root UIDs, cgroup CPU/memory/task limits and a negative IP-socket /
filesystem-visibility probe. The CI host launcher also uses a runner-safe
group switch. The checked native Linux workflow passed **172 backend tests,
2 host acceptance tests, the frontend build and Chromium browser coverage**.
This proves only the stated ephemeral GitHub runner configuration; production
host enforcement, power-loss recovery and an independent security assessment
remain open.

A follow-up host acceptance regression now interrogates the **running target
MainPIDs** via the CI host's privileged `/proc` reader. It requires active
seccomp filtering, `NoNewPrivs`, zero effective and bounding capabilities,
and a distinct network and mount namespace for each active target, separate
from the host. These are checks of the disposable runner's live kernel state,
not merely the configured unit text. They complement the negative target-side
IP-socket and filesystem checks, but do **not** certify an independent
production host, untrusted external targets or recovery after power loss.

## B3 durable verification target lifecycle (21 November 2026)

The native replay/reduction worker now registers each disposable PostgreSQL
target in the control database **before** provisioning. The record binds the
target's deterministic UUID to the verification job, its lease generation and
a bounded trial number. On a worker restart, a separate cleanup claim can
identify old targets with expired ownership and force-drop their databases
and roles. Live, unexpired verification claims are excluded from cleanup.
A failed teardown remains `UNKNOWN` for later retry.

The corresponding control schema is **revision 6**. Apply the normal
`python -m clashbolt.policy` or `python -m clashbolt.accounts migrate`
migration before starting updated workers. Regression tests cover lease
fencing, interrupted verification cleanup on a real disposable native cluster,
and tracking of successful replay/reduction targets.

Limits: only worker-queued verification uses this durable target registry.
Standalone CLI replay/reduction still provisions temporary databases without
a durable cleanup record and must run on trusted disposable test systems.
A database lease does not fence already-running external effects, and neither
power failure nor production-host systemd containment is certified.

## B1 and B2/B3 additional bounded fault checks (21 November 2026)

A disposable PostgreSQL test injects failure after migration SQL has created
a table and inserted a quota row. The migration transaction must roll back
**both** changes, leave the previous control schema usable, and permit a
subsequent idempotent migration. This checks atomic failure handling on a
single initialized CI database, not production downgrade or rollback of
previously committed revisions.

Two real POSIX native-worker tests cover crash points while a target is
PROVISIONING (before database creation) and ACTIVE (after database creation).
Each kills the separate claimant process, expires only its CI test lease,
reconciles the registered target (including database and role if present),
rejects the stale result, and requires the replacement lease generation to
produce the sole certified synthetic PASS. The target cleanup invariant is
checked before and after replacement. These are controlled SIGKILL tests,
not host power loss, distributed failover or external application effect
containment. Existing tests continue to enforce global native slots and
strict host isolation.

## B3 native verifier crash-point recovery drill (21 November 2026)

The PostgreSQL integration suite now starts the **real worker verification
path** in a separate POSIX process for a certified native synthetic witness.
The test pauses just after its fresh PostgreSQL replay target is marked ACTIVE,
kills the verifier abruptly (without Python cleanup), and confirms the
disposable target database still exists at the crash point. It then expires
only the test worker lease and reclaims the recorded target using the normal
reconciliation path. Both database and role must be gone; the durable target
record must be CLEANED; a stale verification receipt must be rejected; and
the unfinished verification job remains UNKNOWN instead of being certified.
Failure cleanup also drops any registered fixture targets.

This closes a **bounded, real-process verifier-crash regression** for the
shipped fixture; it is not an independent host-security assessment, a power
loss test, an actual network partition, or proof that expired leases stop
arbitrary in-flight external effects. No external applications or user
programs are accepted by this test. A deliberately killed subprocess is not
a substitute for multi-host failure testing or independent deployment review.

## Strict CI/security regression gates (21 November 2026)

The `.github/workflows/ci.yml` pipeline runs for each push to `main`,
pull requests, on explicit dispatch, and daily. Every required backend,
frontend, Chromium, dependency-audit and real native systemd host job must
succeed before the `release-gate` job can pass. Jobs have finite timeouts,
and superseded runs are cancelled to avoid stale green results.

- Python: Ruff, syntax compilation, `pip check`, strict pytest configuration,
  unexpected-warning failures, 120-second per-test timeouts, ten slowest test
  durations, complete native PostgreSQL integration suite and branch coverage
  of at least 75%. This is a *floor*, not evidence of adequate per-module
  security coverage. The initial measured coverage was 78.77%.
- Security: `pip-audit --strict` against the exact installed, pinned freeze of
  third-party Python packages, plus npm audit failing for high/critical
  advisories. Unknown registry dependencies fail; the unpublished local
  ClashBolt source is checked by tests and lint rather than a PyPI lookup.
- Frontend: committed npm lockfile, `npm ci` for reproducible dependency
  resolution, TypeScript/Vite build, and multiple real Chromium modes
  including accounts, native worker and same-origin deployment.
- Workflow supply chain: checkout and Python/Node setup actions are pinned to
  verified SHA commits of their official Node24-compatible release refs; the
  workflow retains contents-read-only permissions and does not persist
  checkout credentials.
- Native host: a systemd-backed execution acceptance run validates distinct
  non-root fixture UIDs, CPU/memory/task quotas and negative filesystem/network
  checks on a disposable Linux CI host.

The new strict run exposed a **real unclosed stdout-pipe deadlock** in the
fixed-child worker's oversized-output error path. The worker now terminates
the child and discards remaining pipe bytes in bounded chunks before reaping,
with 256 KiB and 2 MiB regression cases. Do not weaken this safety check.

**Still open:** `main` has no required-status-check protection and existing
repository policy explicitly permits direct-to-main commits; these CI gates
therefore report failures *after* a push, not before it. There is no automatic
production deployment, so `release-gate` is an evidence gate, not a release
authorization. The Python dependency graph is not yet locked across CI runs;
independent production-host security review, higher per-module coverage and
external-target research validation remain unverified. No CI system can
guarantee discovery of every defect or vulnerability.


## A5 agent context corruption quarantine (21 November 2026)

Policy schema revision **9** adds an indexed integrity-check timestamp for
active agent plans. Each worker maintenance pass checks at most 20 eligible
plans whose records have not been checked in the past 30 seconds. Checks
compare the persisted plan and approved oracle binding against the
append-only digest-linked decision history. Only `VERIFIED` entries remain
eligible; corrupted or pre-history `UNCONFIRMED` records transition
atomically to terminal `UNKNOWN` with a persisted evidence gap and
`quarantined` marker. A user can inspect the original inconsistent history,
but cannot authorize a tool receipt or replan from quarantined context.
The semantic frontier ignores quarantined observations.

Real PostgreSQL regressions verify stale proposal quarantine, in-flight
receipt rejection, scan rotation, concurrent sweeps and a separate
`python -m clashbolt.worker --once` process performing recovery. Existing
valid prior feedback paths remain available. This is control-plane
corruption detection, **not** protection from a database superuser capable
of rebuilding the hash chain and its stored data, and not independent
model-context-signature evidence.

## A3/P4 account-bound tool authority and live UI role synchronization (21 November 2026)

Account revision **5** attaches the account UUID and fixed `local` project
to each newly issued named-account capability. The capability broker rechecks
the issuing operator's active account and current membership using PostgreSQL
row locks **before** locking/debiting the capability; the lock order matches
role revocation and avoids cap/account deadlocks. Account disablement, logout,
password reset, project revocation and downgrade permanently revoke tokens.
Switching an old local/private bearer into account mode (or vice versa) is
rejected. HTTP and concurrency regressions exercise the actual capability-only
endpoint, including previously issued tokens after a re-grant.

React polls the authoritative role and removes obsolete operator controls or
clears state after a revoked grant. Chromium exercises a real CLI downgrade
and membership removal against the PostgreSQL-backed API. The sidebar's
previous `Later` placeholders are replaced with links to existing timeline
and agent planning panels, only when those panels are available.

Python CI now runs **Vulture** at 100% confidence after Ruff and before pytest;
TypeScript's strict unused-symbol checks remain enabled. High-confidence
static analysis can miss dynamic/indirect dead code, so neither tool
demonstrates a mathematically dead-code-free repository. Changes should not
add dormant scaffolding, unused helpers, fake UI controls or unverified
production claims. No Docker dependency was introduced.

**Outstanding:** capability execution remains limited to the shipped
single-project synthetic fixture. Authoritative multi-project resource
ownership, per-project worker fairness, genuine external target repro,
independently adjudicated held-out AI experiments and real failover trials
are still incomplete.

## P4 single-project role grants and B2 live claimant suspension (21 November 2026)

- Account schema revision **4** persists membership in the existing `local`
  workspace and records grant/change/revocation through an append-only,
  database-trigger-protected audit. New local accounts receive only the
  account-role-matching grant; upgrading existing schema-3 accounts backfills
  matching local grants **once**, never during later migrations. CLI commands
  `grant-local` and `revoke-local` require trusted local administration.
- The API reads the effective project role on every protected request and
  denies login or resource access without a local membership. An existing
  signed session cannot bypass revocation; reviewer and operator authority
  cannot be swapped or escalated through a project grant. Tests exercise
  session revocation, read/write downgrades, grant ceilings, a foreign-project
  membership that cannot access local resources, and append-only audit.
  The first strict CI run found a write-authorization check that accidentally
  used the legacy account-wide role after the project role had been downgraded.
  It has been corrected to check the live effective role, with separate
  operator-write and reviewer-approval downgrade regressions.
- B2 adds a POSIX SIGSTOP/SIGCONT test with a real claimant process that
  resumes after its lease expires and a replacement publishes a certified
  result; the old generation's receipt must be rejected without a duplicate
  terminal transition.

**Scope guard:** This is not general multi-project tenancy. Experiment runs,
oracle proposals, worker dispatch, agent state and SSE projections are still
hard-coded to the `local` project. Do **not** expose arbitrary project IDs in
HTTP endpoints until every associated database query, resource reference,
background worker, capability and browser projection has been scoped and
cross-tenant tested. B2 process suspension demonstrates control-plane receipt
fencing only, not containment of any already-in-flight external effect.


## B7 bounded concurrent admission regression (21 November 2026)

A disposable-native-PostgreSQL CI test starts 36 concurrent admissions
against a 24-open-run quota, asserts exactly 24 accepted, then competes
36 claimant calls across eight workers. It checks unique claim ownership,
one accepted terminal transition per run and reuse of freed admission capacity.
It is an integration load boundary, **not** a measured production SLO or
evidence of indefinite multi-project fairness. Provider, CPU and systemd
cost accounting remain open. Security-relevant queue tests stay mandatory
within the fail-closed CI release gate.

The P4 local-project grant tests also inject a run belonging to a different
project ID and reject local resource GET, evidence export, transition reads
and cancellation by its UUID. Other-project operations are still disabled;
a full tenant-aware application needs scoped policy, tool and worker
relationships rather than this single-project boundary alone.


## B1 control-primary crash and B4 ungated synthetic measurements (21 November 2026)

The CI-only `ci/check-control-crash-recovery.py` prepares an acknowledged
run and lease on the disposable `clashbolt_test` control PostgreSQL
cluster, then the CI workflow performs an immediate PostgreSQL cluster
stop/restart. The verifier checks persisted run and transition identity,
artificially expires the previous lease for test speed, claims a new
generation and rejects the stale as well as duplicate terminal receipt.
Its CLI refuses non-CI execution and a non-loopback or differently named
control database. This tests local crash recovery of **one primary**,
not asynchronous-replica failover, acknowledged-write durability under
network partitions, or a production recovery-time objective.

The trusted native research CLI `python -m clashbolt.ungated --variant
racy --trials 3` runs exactly the shipped inventory fixture with two
competing PostgreSQL clients **without the controlled per-operation
checkpoint release schedule**. Each trial gets an independent disposable
target database and least-privilege role; the checker evaluates only the
observed final state. It reports PASS/FAIL counts but **never assumes a
race was reproduced**, invents a global causal trace or treats these
known synthetic samples as external-target evidence. The locked variant
serves as a known-correct reference. Only 1-20 trials may run per CLI
invocation. Native CI tests exercise both variants and the real CLI,
including database cleanup and no emitted credentials.

These additions improve B1/B2/B4 failure and confirmation evidence, but
do not meet the PDF's general application instrumentation, uncontrolled
reproduction, or independent deployment certification requirements.
