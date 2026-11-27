"""Bounded, reproducible Hypothesis property tests for pure control-plane invariants."""

import asyncio
import copy
import json

from clashbolt.accounting import reconcile_run_accounting
from clashbolt.agent_feedback import decide_feedback, gap_class, summarize_uncertainty
from clashbolt.experiment import check_invariant, execute
from clashbolt.ops_alerts import diagnose_operations
from clashbolt.witness import evidence_digest, validate_contract
from clashbolt.witness_bundle import create_bundle, verify_bundle
from hypothesis import given, settings
from hypothesis import strategies as st

_MAYBE_MEASURE = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=6),
    st.floats(min_value=-50000.0, max_value=50000.0, allow_nan=False),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
    st.integers(min_value=-1000, max_value=50000),
)

_EXEC_WITNESSES = {
    (variant, schedule): asyncio.run(execute(variant, schedule))
    for variant in ("racy", "locked")
    for schedule in ("overlap", "serial")
}


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    execution_backend=st.sampled_from(["memory", "native-postgresql"]),
    step_budget=st.integers(min_value=4, max_value=32),
    reserved_cpu_ms=st.integers(min_value=500, max_value=30000),
    terminal_status=st.sampled_from(["COMPLETE", "UNKNOWN"]),
    cancel_requested=st.booleans(),
    valid_witness=st.booleans(),
    consumed_steps=st.one_of(st.none(), st.integers(min_value=-5, max_value=40), st.booleans()),
    worker_cpu_ms=_MAYBE_MEASURE,
    child_cpu_ms=_MAYBE_MEASURE,
    wall_ms=_MAYBE_MEASURE,
    include_db=st.booleans(),
    db_active=_MAYBE_MEASURE,
    db_int=st.one_of(st.none(), st.integers(min_value=-3, max_value=500)),
)
def test_reconcile_run_accounting_invariants(
    execution_backend,
    step_budget,
    reserved_cpu_ms,
    terminal_status,
    cancel_requested,
    valid_witness,
    consumed_steps,
    worker_cpu_ms,
    child_cpu_ms,
    wall_ms,
    include_db,
    db_active,
    db_int,
):
    measurements: dict = {
        "worker_cpu_ms": worker_cpu_ms,
        "child_cpu_ms": child_cpu_ms,
        "wall_ms": wall_ms,
    }
    if include_db:
        measurements["database"] = {
            "active_ms": db_active,
            "xact_commit": db_int,
            "xact_rollback": db_int,
            "blks_hit": db_int,
            "blks_read": db_int,
            "database_bytes": db_int,
        }
    state, doc = reconcile_run_accounting(
        execution_backend=execution_backend,
        step_budget=step_budget,
        reserved_cpu_ms=reserved_cpu_ms,
        terminal_status=terminal_status,
        cancel_requested=cancel_requested,
        valid_witness=valid_witness,
        consumed_steps=consumed_steps,
        measurements=measurements,
    )
    assert doc["estimated"]["provider_cost_usd"] is None
    assert doc["estimated"]["status"] == "UNAVAILABLE"
    assert doc["reconciliation"]["status"] == state

    process = doc["measured"]["process"]
    app = doc["measured"]["application"]
    db = doc["measured"]["database"]
    if execution_backend == "memory":
        assert db["status"] == "NOT_APPLICABLE"
        assert db["active_ms"] is None

    if cancel_requested or not valid_witness or terminal_status == "UNKNOWN":
        assert state == "UNCERTAIN"
        assert doc["reconciliation"]["uncertainty_reason"] is not None
    elif (
        process["status"] == "MEASURED"
        and app["status"] == "MEASURED"
        and db["status"] in ("MEASURED", "NOT_APPLICABLE")
    ):
        assert state == "RECONCILED"
        assert doc["reconciliation"]["uncertainty_reason"] is None
        assert doc["reconciliation"]["overspent"] is bool(
            doc["reconciliation"]["overspend_reasons"]
        )
    else:
        assert state == "PARTIAL"
        assert doc["reconciliation"]["uncertainty_reason"] is not None

    if doc["reconciliation"]["overspent"] is False:
        assert doc["reconciliation"]["overspend_reasons"] == []
    if doc["reconciliation"]["overspend_reasons"]:
        assert doc["reconciliation"]["overspent"] is True


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    plan_state=st.sampled_from(["PROPOSED", "IN_FLIGHT", "COMPLETE", "UNKNOWN"]),
    verdict=st.sampled_from(["PASS", "FAIL", "UNKNOWN", None]),
    quarantined=st.booleans(),
    reasons=st.lists(
        st.sampled_from([
            "schema incompatible v2",
            "missing receipt",
            "withdrawn oracle rule",
            "incomplete observation",
            "conflicting values",
        ]),
        max_size=4,
    ),
    budget_remaining=st.integers(min_value=0, max_value=32),
    variant=st.sampled_from(["racy", "locked"]),
    schedule=st.sampled_from(["overlap", "serial"]),
)
def test_uncertainty_and_feedback_invariants(
    plan_state, verdict, quarantined, reasons, budget_remaining, variant, schedule
):
    gaps = [{"classification": gap_class(r), "reason": r} for r in reasons]
    plan = {
        "state": plan_state,
        "variant": variant,
        "schedule": schedule,
        "result": {"verdict": verdict, "quarantined": quarantined},
    }
    summary = summarize_uncertainty(plan, gaps, {"remaining_budget": budget_remaining})
    assert summary["calibrated_confidence"] is None
    assert summary["checker_verdict_authoritative"] is True
    assert summary["probe_budget"]["automatically_authorized"] is False
    assert summary["gap_count"] == len(gaps)
    if not quarantined and plan_state == "COMPLETE" and verdict in ("PASS", "FAIL") and not gaps:
        assert summary["abstention_required"] is False
        assert summary["uncertainty_state"] == "CERTIFIED"
    else:
        assert summary["abstention_required"] is True

    decision = decide_feedback(plan, {"verdict": verdict or "UNKNOWN"}, budget_remaining)
    assert decision["automatically_authorized"] is False
    assert decision["next_step_cost"] in (0, 4)


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    variant=st.sampled_from(["racy", "locked"]),
    schedule=st.sampled_from(["overlap", "serial"]),
    tamper_index=st.integers(min_value=0, max_value=3),
)
def test_witness_and_bundle_roundtrip_and_tamper_invariants(
    variant, schedule, tamper_index
):
    witness = copy.deepcopy(_EXEC_WITNESSES[(variant, schedule)])
    assert validate_contract(witness) is None
    assert evidence_digest(witness) == witness["evidence_sha256"]

    bundle_a = create_bundle(witness)
    bundle_b = create_bundle(witness)
    assert bundle_a == bundle_b
    replayed = asyncio.run(verify_bundle(bundle_a))
    assert replayed["status"] == "REPRODUCED"

    tampered = copy.deepcopy(witness)
    step = tampered["observed_steps"][tamper_index]
    if "observed_stock" in step:
        step["observed_stock"] = int(step["observed_stock"]) + 99
    else:
        step["accepted"] = not bool(step["accepted"])
    tampered["evidence_sha256"] = evidence_digest(tampered)
    assert validate_contract(tampered) is not None
    # Invalid canonical JSON contract stays rejected across dumps/loads
    assert json.loads(json.dumps(tampered))["evidence_sha256"] == tampered["evidence_sha256"]


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    pending=st.integers(min_value=0, max_value=5),
    running=st.integers(min_value=0, max_value=5),
    cancelling=st.integers(min_value=0, max_value=5),
    unknown=st.integers(min_value=0, max_value=5),
    verif_pending=st.integers(min_value=0, max_value=5),
    verif_unknown=st.integers(min_value=0, max_value=5),
    responsive=st.integers(min_value=0, max_value=3),
    oldest_pending=st.floats(min_value=0.0, max_value=300.0, allow_nan=False),
    quarantined=st.integers(min_value=0, max_value=3),
    overspent=st.integers(min_value=0, max_value=3),
    uncertain=st.integers(min_value=0, max_value=3),
    oldest_cancel=st.one_of(st.none(), st.floats(min_value=0.0, max_value=120.0, allow_nan=False)),
)
def test_ops_alerts_purity_and_delivery_invariants(
    pending,
    running,
    cancelling,
    unknown,
    verif_pending,
    verif_unknown,
    responsive,
    oldest_pending,
    quarantined,
    overspent,
    uncertain,
    oldest_cancel,
):
    snapshot = {
        "responsive_workers": responsive,
        "runs": {
            "PENDING": pending,
            "RUNNING": running,
            "CANCELLING": cancelling,
            "UNKNOWN": unknown,
        },
        "verifications": {"PENDING": verif_pending, "UNKNOWN": verif_unknown},
        "oldest_pending_seconds": oldest_pending,
        "quarantined_agent_plans": quarantined,
        "resources": {
            "runs_7d": {"overspent_count": overspent, "uncertain_count": uncertain},
            "model_budget": {"calls_unknown": 0},
        },
        "cancellation": {"oldest_in_flight_seconds": oldest_cancel},
    }
    before = copy.deepcopy(snapshot)
    report = diagnose_operations(snapshot)
    assert snapshot == before
    assert report["notifications_sent"] is False
    assert report["automatic_remediation"] is False
    assert report["delivery"]["mode"] == "IN_APP_POLLING_ONLY"
    assert report["delivery"]["external_channels_configured"] is False
    assert report["status"] == ("ATTENTION" if report["alerts"] else "NORMAL")


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    complete=st.booleans(),
    initial=st.one_of(st.integers(min_value=-2, max_value=10), st.booleans(), st.none()),
    remaining=st.one_of(st.integers(min_value=-2, max_value=10), st.floats(0, 5, allow_nan=False)),
    confirmed=st.one_of(st.integers(min_value=-2, max_value=10), st.text(max_size=3)),
)
def test_check_invariant_contract(complete, initial, remaining, confirmed):
    state = {
        "initial_stock": initial,
        "remaining_stock": remaining,
        "confirmed_count": confirmed,
    }
    verdict = check_invariant(state, complete=complete)
    if not complete or any(type(v) is not int for v in (initial, remaining, confirmed)):
        assert verdict == "UNKNOWN"
    elif min(initial, remaining, confirmed) < 0:
        assert verdict == "FAIL"
    elif confirmed <= initial and remaining == initial - confirmed:
        assert verdict == "PASS"
    else:
        assert verdict == "FAIL"
