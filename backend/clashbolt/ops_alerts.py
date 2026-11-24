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
    return {
        "status": "ATTENTION" if alerts else "NORMAL",
        "alerts": alerts,
        "notifications_sent": False,
        "automatic_remediation": False,
    }
