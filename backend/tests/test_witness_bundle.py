import asyncio
import hashlib
import io
import json
import subprocess
import sys
import zipfile

import pytest
from clashbolt.experiment import execute
from clashbolt.witness_bundle import create_bundle, verify_bundle


def test_data_only_bundle_replays_in_a_new_python_process(tmp_path):
    witness = asyncio.run(execute("racy", "overlap"))
    payload = create_bundle(witness)
    path = tmp_path / "fixture-witness.zip"
    path.write_bytes(payload)
    process = subprocess.run(
        [sys.executable, "-m", "clashbolt.witness_bundle", "verify", str(path)],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["status"] == "REPRODUCED"
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json", "witness.json", "README.txt"
        }


def test_bundle_bytes_and_zip_metadata_are_deterministic():
    witness = asyncio.run(execute("racy", "overlap"))
    first = create_bundle(witness)
    second = create_bundle(witness)
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.compress_type == zipfile.ZIP_STORED
            assert entry.file_size <= 100_000


def test_tampered_or_conflicting_archives_are_invalid():
    original = create_bundle(asyncio.run(execute("racy", "overlap")))
    archive = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("manifest.json", source.read("manifest.json"))
            data = json.loads(source.read("witness.json"))
            data["final_state"]["confirmed_count"] = 999
            output.writestr("witness.json", json.dumps(data))
            output.writestr("README.txt", source.read("README.txt"))
    assert asyncio.run(verify_bundle(archive.getvalue()))["status"] == "INVALID"
    assert asyncio.run(verify_bundle(b"not a zip"))["status"] == "INVALID"
    assert asyncio.run(verify_bundle(b"x" * 1_000_001))["status"] == "INVALID"


def test_incomplete_evidence_cannot_be_packaged():
    witness = asyncio.run(execute("racy", "overlap"))
    witness["complete"] = False
    try:
        create_bundle(witness)
    except ValueError:
        pass
    else:
        raise AssertionError("Incomplete evidence was accepted")


def _replace_witness(contents, value):
    with zipfile.ZipFile(io.BytesIO(contents)) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    raw = json.dumps(value).encode()
    manifest = json.loads(entries["manifest.json"])
    manifest["witness_file_sha256"] = hashlib.sha256(raw).hexdigest()
    entries.update({"witness.json": raw, "manifest.json": json.dumps(manifest)})
    updated = io.BytesIO()
    with zipfile.ZipFile(updated, "w") as bundle:
        for name, payload in entries.items():
            bundle.writestr(name, payload)
    return updated.getvalue()


@pytest.mark.parametrize("value", [None, [], {"oracle": ["bad"]}, {"oracle": "bad"}])
def test_malformed_witness_is_rejected_as_data(value):
    original = create_bundle(asyncio.run(execute("locked", "serial")))
    result = asyncio.run(verify_bundle(_replace_witness(original, value)))
    assert result["status"] == "INVALID"


def test_archive_with_unexpected_entry_is_rejected():
    original = io.BytesIO(create_bundle(asyncio.run(execute("locked", "serial"))))
    with zipfile.ZipFile(original, "a") as archive:
        archive.writestr("unexpected.txt", "not part of the verifier contract")
    assert asyncio.run(verify_bundle(original.getvalue())) == {
        "status": "INVALID", "reason": "Unexpected archive entries",
    }


def test_bundle_cli_reports_invalid_data_and_missing_path(tmp_path, monkeypatch, capsys):
    from clashbolt.witness_bundle import main

    path = tmp_path / "local.zip"
    monkeypatch.setattr(sys, "argv", ["witness_bundle", "verify", str(path)])
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "ZIP bundle" in capsys.readouterr().err
    path.write_bytes(b"not a zip")
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID"
