import asyncio

from clashbolt.experiment import execute
from clashbolt.reducer import reduce_witness


def test_racy_overlapping_witness_has_no_removable_gate():
    witness = asyncio.run(execute("racy", "overlap"))
    report = reduce_witness(witness)
    assert report["status"] == "MINIMAL"
    assert report["original_gate_count"] == 4
    assert report["minimal_gate_count"] == 4
    assert len(report["reduction_trials"]) == 4
    assert not any(trial["accepted"] for trial in report["reduction_trials"])
    assert len(report["report_sha256"]) == 64


def test_corrected_fixture_and_tampered_history_cannot_be_reduced():
    passed = asyncio.run(execute("locked", "overlap"))
    assert reduce_witness(passed)["status"] == "UNCONFIRMED"
    forged = asyncio.run(execute("racy", "overlap"))
    forged["final_state"]["remaining_stock"] = 123
    assert reduce_witness(forged)["status"] == "INVALID"


def test_native_witness_requires_native_replay_cluster():
    witness = asyncio.run(execute("racy", "overlap"))
    witness["execution_backend"] = "native-postgresql"
    from clashbolt.witness import evidence_digest

    witness["evidence_sha256"] = evidence_digest(witness)
    assert reduce_witness(witness)["status"] == "UNCONFIRMED"


def test_reducer_missing_read_returns_unknown_and_respects_trial_cap():
    from clashbolt.reducer import _probe, _trial

    assert _probe("racy", ["a.commit"], None) == "UNKNOWN"
    assert _trial("racy", "overlap", ["a.read", "a.commit"], None) == "PASS"
