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
- **Do not lie about completion.** Note exact implemented features, test
  evidence and remaining gaps in the roadmap. Never claim unmeasured benchmark
  scores or production certification.
- **Workflow:** commit directly to `main` in
  `https://github.com/yashaskn8/ClashBolt`. Do not create branches or PRs.
- **Deployment:** native Python, PostgreSQL and Node only. No Docker, Docker
  Compose, Kubernetes, Temporal, Redis or Kafka for this project.

