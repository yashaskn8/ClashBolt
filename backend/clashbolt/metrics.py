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
    ))
    return "\n".join(lines) + "\n"
