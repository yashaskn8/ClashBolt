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
