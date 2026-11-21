"""Data-only portable package for fixed-fixture replay and integrity checking.

No untrusted scripts, archive filenames or network targets are ever executed.
An installed ClashBolt verifier is required; this is not a self-contained VM.
"""

import argparse
import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

from clashbolt.witness import validate_contract, verify

README = (
    "ClashBolt synthetic evidence bundle v1\n"
    "This package is DATA ONLY; it contains no executable target.\n"
    "Install the matching ClashBolt Python package and run:\n"
    "  python -m clashbolt.witness_bundle verify <bundle.zip>\n"
    "If this uses native-postgresql, separately provision the native target\n"
    "cluster and CLASHBOLT_TARGET_ADMIN_URL / CLASHBOLT_DATABASE_URL.\n"
    "Reproduction is controlled synthetic replay, NOT external bug proof.\n"
)
EXPECTED_FILES = {"manifest.json", "witness.json", "README.txt"}


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def create_bundle(witness: dict) -> bytes:
    rejected = validate_contract(witness)
    if rejected or not witness.get("complete"):
        raise ValueError(rejected or "Incomplete synthetic evidence")
    packed = _json(witness)
    if len(packed) > 100_000:
        raise ValueError("Synthetic witness exceeds 100 KB")
    manifest = {
        "format": "clashbolt-synthetic-bundle-v1",
        "scope": "SYNTHETIC_ONLY",
        "target_revision": witness["target_revision"],
        "oracle_sha256": witness["oracle"]["sha256"],
        "evidence_sha256": witness["evidence_sha256"],
        "witness_file_sha256": hashlib.sha256(packed).hexdigest(),
        "execution_backend": witness.get("execution_backend", "memory"),
    }
    # Use fixed metadata and uncompressed entries. ZIP defaults to today's
    # timestamps, which makes the artifact SHA differ across clean machines.
    # This synthetic bundle is small enough to prefer byte-for-byte stability.
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name, payload in (
            ("manifest.json", _json(manifest)),
            ("witness.json", packed),
            ("README.txt", README.encode()),
        ):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_STORED
            entry.external_attr = 0o644 << 16
            bundle.writestr(entry, payload)
    return output.getvalue()


async def verify_bundle(contents: bytes) -> dict:
    if len(contents) > 1_000_000:
        return {"status": "INVALID", "reason": "Bundle exceeds 1 MB"}
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as bundle:
            entries = bundle.infolist()
            if (
                len(entries) != len(EXPECTED_FILES)
                or {entry.filename for entry in entries} != EXPECTED_FILES
                or any(
                    entry.file_size > 100_000 or entry.is_dir()
                    for entry in entries
                )
            ):
                return {"status": "INVALID", "reason": "Unexpected archive entries"}
            manifest = json.loads(bundle.read("manifest.json"))
            raw = bundle.read("witness.json")
            witness = json.loads(raw)
            if not isinstance(manifest, dict) or not isinstance(witness, dict):
                return {"status": "INVALID", "reason": "Malformed bundle JSON"}
            if (
                manifest.get("format") != "clashbolt-synthetic-bundle-v1"
                or manifest.get("scope") != "SYNTHETIC_ONLY"
                or manifest.get("target_revision") != witness.get("target_revision")
                or manifest.get("oracle_sha256")
                    != (witness.get("oracle") or {}).get("sha256")
                or manifest.get("evidence_sha256") != witness.get("evidence_sha256")
                or manifest.get("execution_backend")
                    != witness.get("execution_backend", "memory")
                or manifest.get("witness_file_sha256")
                    != hashlib.sha256(raw).hexdigest()
            ):
                return {"status": "INVALID", "reason": "Manifest binding mismatch"}
            return await verify(witness)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
        return {"status": "INVALID", "reason": "Malformed synthetic archive"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a data-only ClashBolt bundle")
    parser.add_argument("action", choices=("verify",))
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if not args.archive.is_file() or args.archive.stat().st_size > 1_000_000:
        parser.error("A ZIP bundle smaller than 1 MB is required")
    result = asyncio.run(verify_bundle(args.archive.read_bytes()))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "REPRODUCED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
