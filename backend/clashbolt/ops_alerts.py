"""Read-only, fixed-threshold operational hints; never automate remediation."""


def diagnose_operations(snapshot: dict) -> dict:
    """Thresholds are review prompts, not proven production SLO violations."""
    runs = snapshot["runs"]
    verifications = snapshot["verifications"]
    responsive = snapshot["responsive_workers"]
    alerts = []
    if runs.get("PENDING", 0) > 0 and responsive == 0:
        alerts.append({
            "code": "NO_RESPONSIVE_WORKER",
            "message": "Pending experiments have no recently responsive worker.",
        })
    if snapshot.get("oldest_pending_seconds", 0) >= 120:
        alerts.append({
            "code": "QUEUE_WAIT_OVER_TWO_MINUTES",
            "message": "The oldest pending experiment has waited at least two minutes.",
        })
    if verifications.get("PENDING", 0) > 0 and responsive == 0:
        alerts.append({
            "code": "VERIFICATION_NEEDS_WORKER",
            "message": "Pending verification jobs have no recently responsive worker.",
        })
    if responsive == 0 and (runs.get("RUNNING", 0) > 0 or runs.get("CANCELLING", 0) > 0):
        alerts.append({
            "code": "ACTIVE_RUN_NEEDS_REVIEW",
            "message": "Active experiments have no recently responsive worker; review recovery.",
        })
    if responsive == 0 and verifications.get("RUNNING", 0) > 0:
        alerts.append({
            "code": "ACTIVE_VERIFICATION_NEEDS_REVIEW",
            "message": "Active verification jobs lack a responsive worker; review recovery.",
        })
    if snapshot.get("quarantined_agent_plans", 0) > 0:
        alerts.append({
            "code": "QUARANTINED_AGENT_CONTEXT",
            "message": "Invalid agent history was quarantined; inspect plan evidence gaps.",
        })
    if runs.get("UNKNOWN", 0) > 0:
        alerts.append({
            "code": "UNKNOWN_RUN_OUTCOMES",
            "message": "Experiments have unknown outcomes; review their recorded evidence.",
        })
    if verifications.get("UNKNOWN", 0) > 0:
        alerts.append({
            "code": "UNKNOWN_VERIFICATION_OUTCOMES",
            "message": "Verification outcomes are unknown; review their recorded evidence.",
        })
    resources = snapshot.get("resources")
    if isinstance(resources, dict):
        runs_7d = resources.get("runs_7d") or {}
        if runs_7d.get("overspent_count", 0) > 0:
            alerts.append({
                "code": "RESOURCE_OVERSPEND_DETECTED",
                "message": (
                    "Recent runs exceeded their reserved resource budget; review accounting."
                ),
            })
        model_budget = resources.get("model_budget") or {}
        if runs_7d.get("uncertain_count", 0) > 0 or model_budget.get("calls_unknown", 0) > 0:
            alerts.append({
                "code": "UNCERTAIN_RESOURCE_ACCOUNTING",
                "message": (
                    "Resource expenditure is uncertain for incomplete or unknown executions."
                ),
            })
    cancellation = snapshot.get("cancellation")
    if isinstance(cancellation, dict):
        oldest_cancel = cancellation.get("oldest_in_flight_seconds")
        if oldest_cancel is not None and oldest_cancel >= 30:
            alerts.append({
                "code": "CANCELLATION_PENDING_TOO_LONG",
                "message": (
                    "An in-flight cancellation has waited at least 30 seconds for reconciliation."
                ),
            })
    return {
        "status": "ATTENTION" if alerts else "NORMAL",
        "alerts": alerts,
        "notifications_sent": False,
        "automatic_remediation": False,
        "delivery": {
            "mode": "IN_APP_POLLING_ONLY",
            "project_scoped": True,
            "external_channels_configured": False,
            "reliable_outbound_delivery": False,
            "reason": (
                "Operational review hints are served only via authenticated project "
                "polling; no external alerting transport is configured."
            ),
        },
    }
