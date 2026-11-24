# Data-only held-out comparison protocol

`python -m clashbolt.heldout manifest.json observations.json` accepts only
JSON; it does not download, invoke, or execute targets or models.

A manifest uses protocol `clashbolt-heldout-comparison-v1`, a stable
`suite_id`, nonoverlapping `training_targets` and `heldout_targets` (at
least two), ordered unique integer `seeds`, the fixed strategy list
`["semantic", "random", "coverage", "template", "hypothesis"]`, and declared
integer `cpu_seconds_per_trial` and `model_tokens_per_trial` ceilings.

The observation file contains `manifest_sha256`, calculated by canonical
sorted-key compact JSON, plus a complete `observations` array. Every
(target, seed, strategy) combination must appear exactly once and each row
must contain `target_id`, `seed`, `strategy`, `status` (COMPLETE/UNKNOWN),
`bug_ids` (data-only identifiers), numeric `cpu_seconds`, integer
`model_tokens` and an optional 64-character lowercase
`evidence_sha256`. Nonempty bug IDs require a COMPLETE status and a digest.

The report deduplicates reported bug IDs per target, counts UNKNOWN trials,
reports observed costs, and computes a paired descriptive bootstrap interval
by target against the strongest *reported* baseline. This interval is labelled
`reported_findings_only`: it describes reported findings, not latent outcomes.
Each strategy also counts reported-positive targets, completed targets without
reported findings, and unresolved targets (no reported finding and at least
one UNKNOWN trial). For each fixed baseline, `paired_outcome_uncertainty`
reports resolved/unresolved pair counts and the minimum and maximum possible
paired mean difference when unresolved binary outcomes are allowed to vary.
These are sensitivity bounds, not confidence intervals. Neither the
descriptive interval nor these bounds constitute independent evidence of
discovery or a publication gate. A digest is **not an independent
signature**; anyone able to rewrite both files can forge their contents.
The tool does not validate witness bytes, preregistration timestamps,
actual CPU/provider billing or independent adjudication. It always reports
`promotion_eligible: false`.

A genuine S1 research result still requires a frozen externally registered
corpus, human-approved business rules, verified independent witnesses,
uncontrolled reproduction where relevant, equal combined CPU/model budgets,
blind held-out scoring and review of false positives.
