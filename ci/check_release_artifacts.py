"""Validate independently built ClashBolt wheel, source archive and static UI.

Read-only checks on local release outputs; this never publishes or deploys.
The actual wheel and sdist are also installed in fresh virtual environments
by the mandatory packaging CI job.
"""

import hashlib
import json
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

MODULES = ("api.py", "worker.py", "target_service.py", "witness.py")
BLOCKED_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx", ".map")
MAX_FILE_BYTES = 20_000_000


def _safe_names(names: list[str]) -> None:
    if len(names) != len(set(names)):
        raise ValueError("Duplicate member in release archive")
    for name in names:
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe release archive path")
        if any(part.startswith(".env") for part in path.parts):
            raise ValueError("Environment file included in release")
        if path.suffix.lower() in BLOCKED_SUFFIXES:
            raise ValueError("Sensitive or source-map file included in release")


def validate(wheel: Path, source: Path, frontend: Path) -> dict:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError("A built Python wheel is required")
    if not source.is_file() or not source.name.endswith(".tar.gz"):
        raise ValueError("A built Python source distribution is required")
    if not frontend.is_dir():
        raise ValueError("Built production frontend is required")
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        _safe_names(members)
        metadata = [n for n in members if n.endswith(".dist-info/METADATA")]
        records = [n for n in members if n.endswith(".dist-info/RECORD")]
        if len(metadata) != 1 or len(records) != 1:
            raise ValueError("Wheel requires unique metadata and RECORD")
        meta = Parser().parsestr(archive.read(metadata[0]).decode("utf-8"))
        if meta.get("Name", "").lower() != "clashbolt" or not meta.get("Version"):
            raise ValueError("Unexpected built Python project metadata")
        wheel_files = {}
        for module in MODULES:
            path = "clashbolt/" + module
            if path not in members:
                raise ValueError(f"Critical module missing from wheel: {path}")
            wheel_files[module] = archive.read(path)
        if any("/tests/" in "/" + n or "__pycache__" in n for n in members):
            raise ValueError("Wheel includes tests or bytecode")

    with tarfile.open(source, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        _safe_names(names)
        if any(not item.isfile() and not item.isdir() for item in members):
            raise ValueError("Source archive contains an unsafe special entry")
        source_files = {}
        for module in MODULES:
            paths = [
                item for item in members
                if item.name.endswith("/backend/clashbolt/" + module)
            ]
            if len(paths) != 1 or paths[0].size > MAX_FILE_BYTES:
                raise ValueError(f"Critical module missing or oversized in source: {module}")
            data = archive.extractfile(paths[0])
            if data is None:
                raise ValueError("Source distribution cannot be read")
            source_files[module] = data.read(MAX_FILE_BYTES + 1)
        if wheel_files != source_files:
            raise ValueError("Wheel and source distribution contain different core code")

    index = frontend / "index.html"
    if not index.is_file():
        raise ValueError("Frontend index.html missing")
    html = index.read_text(encoding="utf-8")
    static_files = [
        path for path in frontend.rglob("*") if path.is_file()
    ]
    if not static_files or not any(
        path.suffix == ".js" and path.relative_to(frontend).parts[0] == "assets"
        for path in static_files
    ):
        raise ValueError("Bundled production JavaScript is missing")
    if "assets/" not in html:
        raise ValueError("Frontend index does not reference compiled assets")
    for path in static_files:
        relative = path.relative_to(frontend)
        if (
            any(part.startswith(".env") for part in relative.parts)
            or relative.suffix in BLOCKED_SUFFIXES
            or "node_modules" in relative.parts
            or path.stat().st_size > MAX_FILE_BYTES
        ):
            raise ValueError("Unsafe asset included in frontend release")

    return {
        "project_version": meta["Version"],
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "frontend_assets": len(static_files),
        "matched_core_modules": len(MODULES),
    }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit(
            "Usage: python ci/check_release_artifacts.py wheel.whl source.tar.gz frontend/dist"
        )
    print(json.dumps(validate(*(Path(arg) for arg in sys.argv[1:])), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
