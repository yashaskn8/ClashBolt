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
        "# HELP clashbolt_quarantined_agent_plans Agent decisions quarantined as UNKNOWN",
        "# TYPE clashbolt_quarantined_agent_plans gauge",
        f'clashbolt_quarantined_agent_plans '
        f'{int(snapshot.get("quarantined_agent_plans", 0))}',
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
    resources = snapshot.get("resources")
    if isinstance(resources, dict):
        runs_7d = resources.get("runs_7d") or {}
        lines.extend((
            "# HELP clashbolt_open_reserved_cpu_ms Open experiment CPU reservation in ms",
            "# TYPE clashbolt_open_reserved_cpu_ms gauge",
            f'clashbolt_open_reserved_cpu_ms {int(resources.get("open_reserved_cpu_ms", 0))}',
            "# HELP clashbolt_max_reserved_cpu_ms Project CPU reservation limit in ms",
            "# TYPE clashbolt_max_reserved_cpu_ms gauge",
            f'clashbolt_max_reserved_cpu_ms {int(resources.get("max_reserved_cpu_ms", 0))}',
            "# HELP clashbolt_runs_overspent_7d Overspent runs in the last 7 days",
            "# TYPE clashbolt_runs_overspent_7d gauge",
            f'clashbolt_runs_overspent_7d {int(runs_7d.get("overspent_count", 0))}',
            "# HELP clashbolt_runs_accounting_uncertain_7d Uncertain accounting runs over 7 days",
            "# TYPE clashbolt_runs_accounting_uncertain_7d gauge",
            f'clashbolt_runs_accounting_uncertain_7d {int(runs_7d.get("uncertain_count", 0))}',
        ))
        for metric_name, field_name, help_text in (
            (
                "clashbolt_measured_worker_cpu_ms_7d",
                "measured_worker_cpu_ms_total",
                "Measured worker process CPU time in ms over 7 days",
            ),
            (
                "clashbolt_measured_app_wall_ms_7d",
                "measured_app_wall_ms_total",
                "Measured application execution wall time in ms over 7 days",
            ),
            (
                "clashbolt_measured_db_active_ms_7d",
                "measured_db_active_ms_total",
                "Measured disposable target PostgreSQL active time in ms over 7 days",
            ),
        ):
            value = runs_7d.get(field_name)
            if value is not None:
                lines.extend((
                    f"# HELP {metric_name} {help_text}",
                    f"# TYPE {metric_name} gauge",
                    f"{metric_name} {max(0.0, float(value)):.3f}",
                ))
    cancellation = snapshot.get("cancellation")
    if isinstance(cancellation, dict):
        lines.extend((
            "# HELP clashbolt_cancel_samples_7d Completed cancellation samples over 7 days",
            "# TYPE clashbolt_cancel_samples_7d gauge",
            f'clashbolt_cancel_samples_7d {int(cancellation.get("sample_count", 0))}',
            "# HELP clashbolt_cancel_in_flight In-flight cancellations awaiting reconciliation",
            "# TYPE clashbolt_cancel_in_flight gauge",
            f'clashbolt_cancel_in_flight {int(cancellation.get("in_flight", 0))}',
        ))
        p95 = cancellation.get("p95_seconds")
        if p95 is not None:
            lines.extend((
                "# HELP clashbolt_cancel_latency_p95_seconds P95 cancellation latency over 7 days",
                "# TYPE clashbolt_cancel_latency_p95_seconds gauge",
                f"clashbolt_cancel_latency_p95_seconds {max(0.0, float(p95)):.3f}",
            ))
    target_reset = snapshot.get("target_reset")
    if isinstance(target_reset, dict):
        lines.extend((
            "# HELP clashbolt_target_reset_samples_7d Cleaned disposable targets over 7 days",
            "# TYPE clashbolt_target_reset_samples_7d gauge",
            f'clashbolt_target_reset_samples_7d {int(target_reset.get("sample_count", 0))}',
            "# HELP clashbolt_target_reset_unknown_7d Target cleanups with unknown status over 7d",
            "# TYPE clashbolt_target_reset_unknown_7d gauge",
            f'clashbolt_target_reset_unknown_7d {int(target_reset.get("unknown_count", 0))}',
        ))
        reset_p95 = target_reset.get("reset_p95_ms")
        if reset_p95 is not None:
            lines.extend((
                "# HELP clashbolt_target_reset_p95_ms P95 disposable target provision+teardown ms",
                "# TYPE clashbolt_target_reset_p95_ms gauge",
                f"clashbolt_target_reset_p95_ms {max(0.0, float(reset_p95)):.3f}",
            ))
    return "\n".join(lines) + "\n"
