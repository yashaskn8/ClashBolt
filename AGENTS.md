# ClashBolt repository working rules

This is the local repository policy for code changes, inspired by simple,
pragmatic engineering guidelines. It is not an official Andrej Karpathy document.

- **Read before editing.** Verify the existing code, `docs/ROADMAP.md` and the
  INTERLEAVE TITAN-20 architecture contracts. Prefer the smallest correct diff.
- **Simplicity first.** Favor plain Python, FastAPI, PostgreSQL, React and
  TypeScript. Avoid speculative abstractions, duplicate services and cleverness.
- **Safety before assertions.** A model proposes; deterministic backend code
  authorizes. Only approved oracle versions may certify durable results.
  Missing, expired or contradictory evidence is `UNKNOWN`.
- **Boundaries remain explicit.** The supported target is a shipped local
  synthetic fixture. A child process is not an OS sandbox. Do not accept
  arbitrary URLs, user scripts, untrusted target code or unaudited tool calls.
- **Small verified commits.** Add focused unit and integration tests, run Ruff,
  pytest, TypeScript/Vite build and check GitHub Actions before reporting
  success. Never equate synthetic replay with independent external reproduction.
- **Fail-closed CI.** Do not suppress a failing test, audit finding, coverage
  gate or host-boundary check to obtain a green workflow. Every backend,
  frontend, Chromium, dependency-security and native host job must pass, as
  must the aggregate release-gate, on the exact commit being reported.
  Diagnose legitimate failures and add regression coverage before fixing.
- **No dormant functionality.** New helpers, routes, schema fields and UI
  controls must be reachable from a supported workflow and covered by tests.
  Remove obsolete placeholders instead of creating unused scaffolding.
  CI enforces Ruff, high-confidence Vulture and TypeScript unused-symbol
  checks; investigate findings rather than adding blanket exclusions.
- **Reproducible frontend.** Keep the npm lockfile in version control; use
  `npm ci` in CI. Keep separate PostgreSQL target and control clusters native.
- **Do not lie about completion.** Note exact implemented features, test
  evidence and remaining gaps in the roadmap. Never claim unmeasured benchmark
  scores or production certification.
- **Workflow:** commit directly to `main` in
  `https://github.com/yashaskn8/ClashBolt`. Do not create branches or PRs.
- **Deployment:** native Python, PostgreSQL and Node only. No Docker, Docker
  Compose, Kubernetes, Temporal, Redis or Kafka for this project.


- **Claims and isolation:** A native PostgreSQL target is a disposable database
  on a *different local cluster*, not a sandbox. Every target instance is
  tied to its worker lease generation and reclaimed after an expired claim.
- **Approval:** Keep the reviewer secret distinct from the executor token.
  New model strategies remain unpromoted until held-out equal-budget evidence.

- **Accounts:** Provision role credentials with the native CLI. A reviewer account
  cannot execute operator actions; submitted reviewer and proposer labels are
  replaced with authenticated local identity names. No public signup.

## Engineering tools

- **CodeGraph:** Run `codegraph init --local-only` once in ClashBolt and
  `codegraph sync` after significant changes. Use `codegraph status`,
  `codegraph cycles` and `codegraph_explore` or the CLI `explore`, `context`,
  `callers`, `callees` and `impact` commands before editing. Confirm critical
  edges against source. Keep the index local to ClashBolt.
- **Ponytail:** Use full mode for implementation and review. In Antigravity,
  use `/ponytail full` and `/ponytail-review`. The plugin also registers
  `/ponytail-audit`, `/ponytail-debt`, `/ponytail-gain` and `/ponytail-help`.
  Use `/ponytail ultra` for demonstrated
  excess complexity. Preserve concurrency, transaction and validation guarantees.
- **Engineering skills:** Antigravity CLI 1.5.6 exposes the `agent-skills`
  plugin as unprefixed slash commands. Use `agy plugin list` to check the
  installed skills. Start with `/using-agent-skills`.
  Use `/incremental-implementation` for changes, `/test-driven-development`
  for behavior changes, `/debugging-and-error-recovery` for reproducible failures,
  `/doubt-driven-development` for consequential assumptions, and
  `/code-review-and-quality` before commits. Apply `/ci-cd-and-automation`,
  `/security-and-hardening`, `/accessibility-engineering`,
  `/browser-testing-with-devtools` and `/frontend-ui-engineering` when their
  areas change. For module design, use `/codebase-design` and
  `/improve-codebase-architecture`.
- **RTK and Context7:** Reuse the registered Antigravity RTK hook. Prefix
  supported commands with `rtk` in shells that run outside that hook. Recover
  full diagnostics with `rtk bypass <command>`. Use Context7
  `resolve-library-id` and `query-docs` for consequential version-specific
  behavior. Send only public library names, versions and documentation questions.
- **Browser verification:** Reuse Antigravity's Playwright MCP registration with
  `--isolated` for interactive checks against disposable loopback environments.
  From `frontend`, run `rtk npm run test:e2e` for automated browser coverage.
- **Architecture and regression checks:** From the root, install the backend
  with `.venv\Scripts\python.exe -m pip install -e ".[dev]"`. Run
  `.venv\Scripts\lint-imports.exe`, `.venv\Scripts\python.exe -m ruff check .`
  and `.venv\Scripts\python.exe -m pytest`. From `frontend`, run `npm ci`,
  `rtk npm run check:architecture` and `rtk npm run build`. On Windows, running
  the architecture check from `frontend` selects the pinned local executable.
  Verify contract changes with isolated failing fixtures. Use bounded Hypothesis
  cases for pure invariants. Use `--hypothesis-seed=<seed>` to repeat a seeded run
  and `--hypothesis-show-statistics` to check generated examples. Verify
  concurrency and transaction behavior with the native PostgreSQL integration
  suite on separate disposable control and target clusters. Check GitHub Actions
  on the exact commit pushed to `main`.
