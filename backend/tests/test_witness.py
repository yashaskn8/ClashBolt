import asyncio
import json
import subprocess
import sys

import pytest
from clashbolt.experiment import execute
from clashbolt.witness import evidence_digest, verify


def test_clean_process_reproduces_controlled_witness(tmp_path):
    evidence = asyncio.run(execute("racy", "overlap"))
    path = tmp_path / "witness.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    outcome = subprocess.run(
        [sys.executable, "-m", "clashbolt.witness", str(path)],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert outcome.returncode == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result["status"] == "REPRODUCED"
    assert result["verdict"] == "FAIL"


def test_changed_payload_and_scope_fail_closed():
    evidence = asyncio.run(execute("locked", "serial"))
    assert asyncio.run(verify(evidence))["status"] == "REPRODUCED"

    tampered = json.loads(json.dumps(evidence))
    tampered["final_state"]["confirmed_count"] = 99
    assert asyncio.run(verify(tampered))["status"] == "INVALID"

    modified_scope = json.loads(json.dumps(evidence))
    modified_scope["scope"] = "EXTERNAL"
    assert asyncio.run(verify(modified_scope))["status"] == "INVALID"

    missing = json.loads(json.dumps(evidence))
    missing.pop("oracle")
    assert asyncio.run(verify(missing))["status"] == "INVALID"


def test_self_consistent_hash_does_not_make_forged_history_valid():
    original = asyncio.run(execute("racy", "overlap"))
    for field, replacement in (
        ("observed_stock", 0),
        ("sequence", 99),
        ("client", "z"),
    ):
        forged = json.loads(json.dumps(original))
        forged["observed_steps"][0][field] = replacement
        forged["evidence_sha256"] = evidence_digest(forged)
        assert asyncio.run(verify(forged))["status"] == "INVALID"


def test_forged_commit_or_state_is_rejected_even_with_new_digest():
    original = asyncio.run(execute("racy", "overlap"))
    forged = json.loads(json.dumps(original))
    forged["observed_steps"][2]["accepted"] = False
    forged["evidence_sha256"] = evidence_digest(forged)
    assert asyncio.run(verify(forged))["status"] == "INVALID"

    forged = json.loads(json.dumps(original))
    forged["final_state"]["remaining_stock"] = 99
    forged["evidence_sha256"] = evidence_digest(forged)
    assert asyncio.run(verify(forged))["status"] == "INVALID"


def test_malformed_types_fail_closed():
    original = asyncio.run(execute("racy", "overlap"))
    malformed = json.loads(json.dumps(original))
    malformed["schedule"] = ["overlap"]
    assert asyncio.run(verify(malformed))["status"] == "INVALID"

    malformed = json.loads(json.dumps(original))
    malformed["observed_steps"][0]["sequence"] = True
    malformed["evidence_sha256"] = evidence_digest(malformed)
    assert asyncio.run(verify(malformed))["status"] == "INVALID"


@pytest.mark.parametrize("section,field,value", [
    ("fixture", "initial_stock", True),
    ("fixture", "quantity_each", 1.0),
    ("final_state", "remaining_stock", False),
    ("final_state", "confirmed_count", 1.0),
])
def test_witness_rejects_rehashed_noninteger_counts(section, field, value):
    evidence = asyncio.run(execute("locked", "serial"))
    evidence[section] = {**evidence[section], field: value}
    evidence["evidence_sha256"] = evidence_digest(evidence)
    assert asyncio.run(verify(evidence))["status"] == "INVALID"


def test_witness_rejects_nonfinite_extras():
    evidence = asyncio.run(execute("locked", "serial"))
    evidence["unexpected"] = float("nan")
    assert asyncio.run(verify(evidence)) == {
        "status": "INVALID", "reason": "Witness cannot be serialized",
    }


def test_witness_cli_handles_deeply_nested_data(tmp_path, monkeypatch, capsys):
    from clashbolt.witness import main

    path = tmp_path / "nested.json"
    path.write_text("[" * 10_000 + "null" + "]" * 10_000, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["witness", str(path)])
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "Cannot parse witness JSON" in capsys.readouterr().err


def test_witness_cli_rejects_duplicate_nested_keys(tmp_path, monkeypatch, capsys):
    from clashbolt.witness import main

    evidence = asyncio.run(execute("locked", "serial"))
    text = json.dumps(evidence).replace(
        '"initial_stock": 1', '"initial_stock": 2, "initial_stock": 1',
    )
    path = tmp_path / "duplicate.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["witness", str(path)])
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "Duplicate evidence field: initial_stock" in capsys.readouterr().err


def test_witness_contract_and_cli_boundary_cases(tmp_path, monkeypatch, capsys):
    from clashbolt import witness as witness_mod

    assert witness_mod.validate_contract(["not-an-object"]) == "Expected a JSON object"
    evidence = asyncio.run(execute("racy", "overlap"))

    bad_count = json.loads(json.dumps(evidence))
    bad_count["observed_steps"] = []
    bad_count["evidence_sha256"] = evidence_digest(bad_count)
    assert "missing checkpoints" in witness_mod.validate_contract(bad_count)

    over_count = json.loads(json.dumps(evidence))
    over_count["observed_steps"].append({"sequence": 5})
    over_count["evidence_sha256"] = evidence_digest(over_count)
    assert "Invalid observed checkpoint count" in witness_mod.validate_contract(over_count)

    non_dict_step = json.loads(json.dumps(evidence))
    non_dict_step["observed_steps"][0] = "bad"
    non_dict_step["evidence_sha256"] = evidence_digest(non_dict_step)
    assert "structured observation" in witness_mod.validate_contract(non_dict_step)

    bad_read_fields = json.loads(json.dumps(evidence))
    bad_read_fields["observed_steps"][0]["extra"] = 1
    bad_read_fields["evidence_sha256"] = evidence_digest(bad_read_fields)
    assert "Unexpected fields in a stock read" in witness_mod.validate_contract(bad_read_fields)

    bad_commit_fields = json.loads(json.dumps(evidence))
    bad_commit_fields["observed_steps"][2]["extra"] = 1
    bad_commit_fields["evidence_sha256"] = evidence_digest(bad_commit_fields)
    assert "Unexpected fields in a commit" in witness_mod.validate_contract(bad_commit_fields)

    no_preceding_read = json.loads(json.dumps(evidence))
    no_preceding_read["requested_gates"] = ["a.commit", "b.read", "a.read", "b.commit"]
    # Keep schedule matching to isolate complete=False incomplete verdict check too.
    incomplete = json.loads(json.dumps(evidence))
    incomplete["complete"] = False
    incomplete["verdict"] = "UNKNOWN"
    incomplete["evidence_sha256"] = evidence_digest(incomplete)
    assert asyncio.run(verify(incomplete))["status"] == "UNCONFIRMED"

    wrong_verdict = json.loads(json.dumps(evidence))
    wrong_verdict["verdict"] = "PASS"
    wrong_verdict["evidence_sha256"] = evidence_digest(wrong_verdict)
    assert "Verdict disagrees" in witness_mod.validate_contract(wrong_verdict)

    # Native verification unconfigured and failing target paths.
    native = json.loads(json.dumps(evidence))
    native["execution_backend"] = "native-postgresql"
    native["evidence_sha256"] = evidence_digest(native)
    monkeypatch.delenv("CLASHBOLT_TARGET_ADMIN_URL", raising=False)
    monkeypatch.delenv("CLASHBOLT_DATABASE_URL", raising=False)
    assert asyncio.run(verify(native))["status"] == "UNCONFIRMED"

    class BrokenTarget:
        def __enter__(self):
            raise RuntimeError("target boom")

        def __exit__(self, *args):
            return False

    assert asyncio.run(verify(native, target_factory=BrokenTarget))["reason"] == (
        "Native target replay failed"
    )

    # CLI checks: missing file, oversized file, valid exit 0, and invalid exit 2.
    monkeypatch.setattr(sys, "argv", ["witness", str(tmp_path / "missing.json")])
    with pytest.raises(SystemExit, match="2"):
        witness_mod.main()
    oversized = tmp_path / "huge.json"
    oversized.write_bytes(b"x" * 100_005)
    monkeypatch.setattr(sys, "argv", ["witness", str(oversized)])
    with pytest.raises(SystemExit, match="2"):
        witness_mod.main()

    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["witness", str(valid_path)])
    assert witness_mod.main() == 0
    bad_path = tmp_path / "invalid.json"
    evidence_bad = {**evidence, "verdict": "PASS"}
    bad_path.write_text(json.dumps(evidence_bad), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["witness", str(bad_path)])
    assert witness_mod.main() == 2
    capsys.readouterr()
