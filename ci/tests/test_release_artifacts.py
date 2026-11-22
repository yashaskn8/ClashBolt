"""Adversarial validation of release artifacts without a publishing endpoint."""

import io
import tarfile
import zipfile

import pytest

from ci.check_release_artifacts import MODULES, validate


def _fixture(tmp_path):
    wheel = tmp_path / "clashbolt-0.2.0-py3-none-any.whl"
    source = tmp_path / "clashbolt-0.2.0.tar.gz"
    frontend = tmp_path / "ui"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text(
        '<script type="module" src="/assets/index-abc.js"></script>',
        encoding="utf-8",
    )
    (assets / "index-abc.js").write_text("console.log('fixed synthetic UI')")

    with zipfile.ZipFile(wheel, "w") as archive:
        for module in MODULES:
            archive.writestr("clashbolt/" + module, "fixed source for " + module)
        archive.writestr(
            "clashbolt-0.2.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: clashbolt\nVersion: 0.2.0\n",
        )
        archive.writestr("clashbolt-0.2.0.dist-info/RECORD", "")
    with tarfile.open(source, "w:gz") as archive:
        for module in MODULES:
            data = ("fixed source for " + module).encode()
            info = tarfile.TarInfo(
                "clashbolt-0.2.0/backend/clashbolt/" + module
            )
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return wheel, source, frontend


def test_valid_release_bundle_requires_matching_built_sources(tmp_path):
    wheel, source, frontend = _fixture(tmp_path)
    report = validate(wheel, source, frontend)
    assert report["matched_core_modules"] == len(MODULES)
    assert report["frontend_assets"] == 2
    assert report["project_version"] == "0.2.0"
    assert len(report["wheel_sha256"]) == 64


@pytest.mark.parametrize("problem", [
    "missing_wheel", "missing_source", "missing_frontend",
    "mismatched_code", "unsafe_wheel_path", "unsafe_tar_path",
    "missing_metadata", "wheel_tests", "frontend_map",
    "frontend_env", "missing_js", "missing_index_reference",
])
def test_release_audit_blocks_malformed_or_sensitive_artifacts(tmp_path, problem):
    wheel, source, frontend = _fixture(tmp_path)
    if problem == "missing_wheel":
        wheel.unlink()
    elif problem == "missing_source":
        source.unlink()
    elif problem == "missing_frontend":
        (frontend / "index.html").unlink()
        (frontend / "assets/index-abc.js").unlink()
        (frontend / "assets").rmdir()
        frontend.rmdir()
    elif problem in ("mismatched_code", "unsafe_tar_path"):
        with tarfile.open(source, "w:gz") as archive:
            for module in MODULES:
                text = (
                    "different" if problem == "mismatched_code"
                    else "fixed source for " + module
                )
                data = text.encode()
                name = (
                    "../escape.py" if problem == "unsafe_tar_path" and module == MODULES[0]
                    else "clashbolt-0.2.0/backend/clashbolt/" + module
                )
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    elif problem in ("unsafe_wheel_path", "missing_metadata", "wheel_tests"):
        with zipfile.ZipFile(wheel, "w") as archive:
            for module in MODULES:
                archive.writestr("clashbolt/" + module, "fixed source for " + module)
            if problem != "missing_metadata":
                archive.writestr(
                    "clashbolt-0.2.0.dist-info/METADATA",
                    "Metadata-Version: 2.4\nName: clashbolt\nVersion: 0.2.0\n",
                )
            archive.writestr("clashbolt-0.2.0.dist-info/RECORD", "")
            if problem == "unsafe_wheel_path":
                archive.writestr("../escape.py", "")
            if problem == "wheel_tests":
                archive.writestr("clashbolt/tests/test_leak.py", "")
    elif problem == "frontend_map":
        (frontend / "assets/index-abc.js.map").write_text("source map")
    elif problem == "frontend_env":
        (frontend / ".env.production").write_text("never publish this")
    elif problem == "missing_js":
        (frontend / "assets/index-abc.js").unlink()
    elif problem == "missing_index_reference":
        (frontend / "index.html").write_text("<html>empty</html>")
    with pytest.raises(ValueError):
        validate(wheel, source, frontend)


def test_release_rejects_duplicate_archive_member_names(tmp_path):
    wheel, source, frontend = _fixture(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("clashbolt/api.py", "shadowed code")
    with pytest.raises(ValueError, match="Duplicate"):
        validate(wheel, source, frontend)
