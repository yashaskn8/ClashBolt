import asyncio
import json
import subprocess
import sys

from clashbolt.experiment import execute
from clashbolt.witness import verify


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
