# ClashBolt

ClashBolt is an AI Concurrency Intelligence research project for **reproducible application-level business-invariant failures** in concurrent APIs.

## Source and scope

The starting architecture is the 28-page **INTERLEAVE TITAN-20 Architecture** (3 November 2026), from before the project was renamed ClashBolt. This repository follows its 20-feature plan: seven backend (B1–B7), seven agent (A1–A7), five product (P1–P5), and one signature (S1). The document is a design proposal, not proof of completed features.

Build in dependency order. The first deliverable is a safe, local-only, synthetic concurrency experiment (B3/B4/B5 + P1/P3 with a deterministic tool boundary), followed by durable control-plane and agent functionality. Never present a simulation as a production concurrency finding.

## Technology and working rules

- Python / FastAPI backend; PostgreSQL durable control state
- React + TypeScript frontend
- No Docker; use native Python, PostgreSQL and Node
- No Temporal, Redis, Kafka or Kubernetes
- Small readable code, explicit contracts, focused tests, no speculative abstractions
- Commit directly on `main`; no feature branches or PRs for this project

## Status

Implementation in progress. See [docs/ROADMAP.md](docs/ROADMAP.md) for scope and verification status after the initial code lands.
