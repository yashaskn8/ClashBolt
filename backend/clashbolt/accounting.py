"""Pure resource reservation, multi-layer reconciliation, and overspend checks.

Distinguishes process-level, application-level, and database-level measurements,
plus external provider cost availability. Missing data is never zero expenditure.
"""

import math

DEFAULT_RESERVED_CPU_MS = 3000
MIN_RESERVED_CPU_MS = 500
MAX_RESERVED_CPU_MS = 30000
DEFAULT_MAX_RESERVED_CPU_MS = 180000

UNAVAILABLE_PROVIDER_COST = {
    "provider_cost_usd": None,
    "status": "UNAVAILABLE",
    "reason": "No metered external provider billing is connected",
}


def _nonneg_float(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    val = float(value)
    if not math.isfinite(val) or val < 0:
        return None
    return round(val, 3)


def _nonneg_int(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _unavailable_db(execution_backend: str) -> dict:
    return {
        "active_ms": None,
        "xact_commit": None,
        "xact_rollback": None,
        "blks_hit": None,
        "blks_read": None,
        "database_bytes": None,
        "provision_ms": None,
        "teardown_ms": None,
        "status": "NOT_APPLICABLE" if execution_backend == "memory" else "UNAVAILABLE",
    }


def _reservation_block(
    *, execution_backend: str, step_budget: int, reserved_cpu_ms: int
) -> dict:
    return {
        "step_budget": step_budget,
        "cpu_ms": reserved_cpu_ms,
        "disposable_databases": int(execution_backend == "native-postgresql"),
    }


def build_reservation(
    *, execution_backend: str, step_budget: int, reserved_cpu_ms: int
) -> dict:
    return {
        "reservation": _reservation_block(
            execution_backend=execution_backend,
            step_budget=step_budget,
            reserved_cpu_ms=reserved_cpu_ms,
        ),
        "measured": {
            "process": {
                "worker_cpu_ms": None,
                "child_cpu_ms": None,
                "total_cpu_ms": None,
                "status": "UNAVAILABLE",
            },
            "application": {
                "consumed_steps": None,
                "wall_ms": None,
                "status": "UNAVAILABLE",
            },
            "database": _unavailable_db(execution_backend),
        },
        "estimated": dict(UNAVAILABLE_PROVIDER_COST),
        "reconciliation": {
            "status": "RESERVED",
            "overspent": None,
            "overspend_reasons": [],
            "cpu_delta_ms": None,
            "uncertainty_reason": (
                "Execution has not completed; actual expenditure is not yet measured"
            ),
        },
    }


def released_run_accounting(
    *, execution_backend: str, step_budget: int, reserved_cpu_ms: int
) -> dict:
    return {
        "reservation": _reservation_block(
            execution_backend=execution_backend,
            step_budget=step_budget,
            reserved_cpu_ms=reserved_cpu_ms,
        ),
        "measured": {
            "process": {
                "worker_cpu_ms": None,
                "child_cpu_ms": None,
                "total_cpu_ms": None,
                "status": "NOT_APPLICABLE",
            },
            "application": {
                "consumed_steps": None,
                "wall_ms": None,
                "status": "NOT_APPLICABLE",
            },
            "database": {
                "active_ms": None,
                "xact_commit": None,
                "xact_rollback": None,
                "blks_hit": None,
                "blks_read": None,
                "database_bytes": None,
                "provision_ms": None,
                "teardown_ms": None,
                "status": "NOT_APPLICABLE",
            },
        },
        "estimated": dict(UNAVAILABLE_PROVIDER_COST),
        "reconciliation": {
            "status": "RELEASED",
            "overspent": False,
            "overspend_reasons": [],
            "cpu_delta_ms": None,
            "uncertainty_reason": None,
        },
    }


def uncertain_run_accounting(
    *, execution_backend: str, step_budget: int, reserved_cpu_ms: int, reason: str
) -> dict:
    doc = build_reservation(
        execution_backend=execution_backend,
        step_budget=step_budget,
        reserved_cpu_ms=reserved_cpu_ms,
    )
    doc["reconciliation"] = {
        "status": "UNCERTAIN",
        "overspent": None,
        "overspend_reasons": [],
        "cpu_delta_ms": None,
        "uncertainty_reason": reason,
    }
    return doc


def reconcile_run_accounting(
    *,
    execution_backend: str,
    step_budget: int,
    reserved_cpu_ms: int,
    terminal_status: str,
    cancel_requested: bool,
    valid_witness: bool,
    consumed_steps: int | None,
    measurements: dict | None,
) -> tuple[str, dict]:
    raw = measurements if isinstance(measurements, dict) else {}
    worker_cpu_ms = _nonneg_float(raw.get("worker_cpu_ms"))
    child_cpu_ms = _nonneg_float(raw.get("child_cpu_ms"))
    if worker_cpu_ms is not None and child_cpu_ms is not None:
        total_cpu_ms: float | None = round(worker_cpu_ms + child_cpu_ms, 3)
        process_status = "MEASURED"
    elif worker_cpu_ms is not None or child_cpu_ms is not None:
        total_cpu_ms = None
        process_status = "PARTIAL"
    else:
        total_cpu_ms = None
        process_status = "UNAVAILABLE"

    app_steps = _nonneg_int(consumed_steps) if valid_witness else None
    wall_ms = _nonneg_float(raw.get("wall_ms"))
    if app_steps is not None and wall_ms is not None:
        app_status = "MEASURED"
    elif app_steps is not None or wall_ms is not None:
        app_status = "PARTIAL"
    else:
        app_status = "UNAVAILABLE"

    if execution_backend == "memory":
        database = _unavailable_db("memory")
    else:
        db_raw = raw.get("database")
        if isinstance(db_raw, dict):
            active_ms = _nonneg_float(db_raw.get("active_ms"))
            xact_commit = _nonneg_int(db_raw.get("xact_commit"))
            xact_rollback = _nonneg_int(db_raw.get("xact_rollback"))
            blks_hit = _nonneg_int(db_raw.get("blks_hit"))
            blks_read = _nonneg_int(db_raw.get("blks_read"))
            db_bytes = _nonneg_int(db_raw.get("database_bytes"))
            provision_ms = _nonneg_float(db_raw.get("provision_ms"))
            teardown_ms = _nonneg_float(db_raw.get("teardown_ms"))
        else:
            active_ms = xact_commit = xact_rollback = blks_hit = blks_read = db_bytes = None
            provision_ms = teardown_ms = None
        if None not in (
            active_ms, xact_commit, xact_rollback, blks_hit, blks_read, db_bytes
        ):
            database = {
                "active_ms": active_ms,
                "xact_commit": xact_commit,
                "xact_rollback": xact_rollback,
                "blks_hit": blks_hit,
                "blks_read": blks_read,
                "database_bytes": db_bytes,
                "provision_ms": provision_ms,
                "teardown_ms": teardown_ms,
                "status": "MEASURED",
            }
        else:
            database = _unavailable_db("native-postgresql")

    overspend_reasons: list[str] = []
    if app_steps is not None and app_steps > step_budget:
        overspend_reasons.append("step_budget_exceeded")
    if total_cpu_ms is not None and total_cpu_ms > reserved_cpu_ms:
        overspend_reasons.append("cpu_reservation_exceeded")
    elif worker_cpu_ms is not None and worker_cpu_ms > reserved_cpu_ms:
        overspend_reasons.append("worker_cpu_reservation_exceeded")

    cpu_delta_ms = (
        round(total_cpu_ms - reserved_cpu_ms, 3) if total_cpu_ms is not None else None
    )

    if cancel_requested or not valid_witness or terminal_status == "UNKNOWN":
        state = "UNCERTAIN"
        overspent: bool | None = True if overspend_reasons else None
        if cancel_requested:
            uncertainty_reason: str | None = (
                "Cancelled during execution; actual expenditure may be incomplete"
            )
        elif not valid_witness:
            uncertainty_reason = (
                "Valid witness missing; application expenditure could not be established"
            )
        else:
            uncertainty_reason = (
                "Run ended UNKNOWN; complete resource expenditure could not be established"
            )
    elif (
        process_status == "MEASURED"
        and app_status == "MEASURED"
        and database["status"] in ("MEASURED", "NOT_APPLICABLE")
    ):
        state = "RECONCILED"
        overspent = bool(overspend_reasons)
        uncertainty_reason = None
    else:
        state = "PARTIAL"
        overspent = True if overspend_reasons else None
        gaps = []
        if process_status != "MEASURED":
            gaps.append("process CPU")
        if app_status != "MEASURED":
            gaps.append("application wall/steps")
        if database["status"] == "UNAVAILABLE":
            gaps.append("target database statistics")
        uncertainty_reason = (
            f"Unavailable resource measurements: {', '.join(gaps)}"
            if gaps else "Partial resource measurements"
        )

    return state, {
        "reservation": _reservation_block(
            execution_backend=execution_backend,
            step_budget=step_budget,
            reserved_cpu_ms=reserved_cpu_ms,
        ),
        "measured": {
            "process": {
                "worker_cpu_ms": worker_cpu_ms,
                "child_cpu_ms": child_cpu_ms,
                "total_cpu_ms": total_cpu_ms,
                "status": process_status,
            },
            "application": {
                "consumed_steps": app_steps,
                "wall_ms": wall_ms,
                "status": app_status,
            },
            "database": database,
        },
        "estimated": dict(UNAVAILABLE_PROVIDER_COST),
        "reconciliation": {
            "status": state,
            "overspent": overspent,
            "overspend_reasons": overspend_reasons,
            "cpu_delta_ms": cpu_delta_ms,
            "uncertainty_reason": uncertainty_reason,
        },
    }
