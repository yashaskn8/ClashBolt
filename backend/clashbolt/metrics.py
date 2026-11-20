"""Minimal local-only Prometheus exposition with no secrets or dependencies."""

RUN_STATES = ("PENDING", "RUNNING", "CANCELLING", "CANCELLED", "COMPLETE", "UNKNOWN")
VERIFY_STATES = ("PENDING", "RUNNING", "COMPLETE", "UNKNOWN")


def operational_metrics(snapshot: dict) -> str:
    """Never interpolate arbitrary DB labels into monitoring output."""
    lines = [
        "# HELP clashbolt_runs Control-plane runs by authoritative state",
        "# TYPE clashbolt_runs gauge",
    ]
    for state in RUN_STATES:
        lines.append(
            f'clashbolt_runs{{state="{state}"}} {int(snapshot["runs"].get(state, 0))}'
        )
    lines.extend((
        "# HELP clashbolt_verification_jobs Verification jobs by state",
        "# TYPE clashbolt_verification_jobs gauge",
    ))
    for state in VERIFY_STATES:
        lines.append(
            f'clashbolt_verification_jobs{{state="{state}"}} '
            f'{int(snapshot["verifications"].get(state, 0))}'
        )
    lines.extend((
        "# HELP clashbolt_responsive_workers Recently responding worker processes",
        "# TYPE clashbolt_responsive_workers gauge",
        f'clashbolt_responsive_workers {int(snapshot["responsive_workers"])}',
        "# HELP clashbolt_worker_records Known workers in the last day",
        "# TYPE clashbolt_worker_records gauge",
        f'clashbolt_worker_records {len(snapshot["workers"])}',
        "# HELP clashbolt_native_slots_occupied Active native target claims",
        "# TYPE clashbolt_native_slots_occupied gauge",
        f'clashbolt_native_slots_occupied {int(snapshot.get("native_slots_occupied", 0))}',
        "# HELP clashbolt_native_slots_limit Configured active native target claim limit",
        "# TYPE clashbolt_native_slots_limit gauge",
        f'clashbolt_native_slots_limit {int(snapshot.get("native_slots_limit", 2))}',
        "# HELP clashbolt_oldest_pending_seconds Oldest pending run age in seconds",
        "# TYPE clashbolt_oldest_pending_seconds gauge",
        f'clashbolt_oldest_pending_seconds '
        f'{max(0.0, float(snapshot.get("oldest_pending_seconds", 0))):.3f}',
        "# HELP clashbolt_run_start_p95_seconds P95 first claim latency over 7 days",
        "# TYPE clashbolt_run_start_p95_seconds gauge",
        f'clashbolt_run_start_p95_seconds '
        f'{max(0.0, float(snapshot.get("start_p95_seconds", 0))):.3f}',
    ))
    return "\n".join(lines) + "\n"
