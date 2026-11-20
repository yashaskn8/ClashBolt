# Native single-host operator checks

ClashBolt is a **local synthetic concurrency lab**, not an externally
accessible testing service. Follow `docs/NATIVE_SERVICE.md` for deployment,
`docs/NATIVE_SETUP.md` for database setup, and the backup/restore guide before
evaluating recovery claims. No Docker is required.

## Review operations

An authenticated operator can inspect the **Recovery Index** in the React
interface or `GET /api/operations`. It includes queue age, 7-day first-claim
P95, live worker heartbeat count, native claim slots, verification jobs and
a small `diagnostics` object with fixed rule-based hints.

The signals are **not** external notifications, pages, production SLO
guarantees, or automatic remediation. A lab without queued work and without
workers is NORMAL. When work is pending with no responsive worker, review the
native worker service, PostgreSQL availability, journal logs and target-cluster
connection without exposing database credentials. If queue age reaches two
minutes, check admission quotas, target cleanup and current target slots.
Review pending verification jobs after the separate worker recovers.

## Ambiguous outcomes

Never infer completion from an expired worker lease, an interrupted target
write, an expired model request or a failed verification. Inspect durable
`UNKNOWN` records and their decision history; preserve original evidence
and avoid automatic replay against any non-disposable application. Revoked
oracle approval withdraws authoritative verdicts.

## Backup and host security

Use the documented private native backup plus **disposable-cluster restore**
drill. Keep API/worker credentials separate, bind only to loopback, and verify
the installed systemd unit sandboxing on the actual host. Configuration-file
checks do not prove kernel-level egress denial. Do not enable untrusted target
execution without independently verified OS/process containment and tenant
authorization. Real multi-node failover and externally verified research
results remain outstanding.
