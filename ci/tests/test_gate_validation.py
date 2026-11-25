"""Adversarial regression tests for CI policy and report gate tampering.

All samples are synthetic; no services, repositories or networks are attacked.
"""

import copy
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from ci.check_ci_policy import audit
from ci.check_quality_report import (
    EXPECTED_SKIPS,
    MIN_TEST_CASES,
    MODULE_MINIMUMS,
    validate,
)

WORKFLOW = Path(".github/workflows/ci.yml")
PLAYWRIGHT = Path("frontend/playwright.config.ts")


def _reports(tmp_path):
    suite = ElementTree.Element(
        "testsuite",
        tests=str(MIN_TEST_CASES), failures="0", errors="0", skipped="2",
    )
    skips = list(EXPECTED_SKIPS)
    for index in range(MIN_TEST_CASES):
        if index < len(skips):
            mod, name = skips[index]
        else:
            mod, name = ("test_synthetic", f"test_fixture_{index}")
        test = ElementTree.SubElement(
            suite, "testcase",
            classname=f"backend.tests.{mod}", name=name,
        )
        if index < len(skips):
            ElementTree.SubElement(test, "skipped")
    names = list(MODULE_MINIMUMS) + [
        f"backend/clashbolt/dummy_{index}.py" for index in range(25 - len(MODULE_MINIMUMS))
    ]
    files = {}
    for index, name in enumerate(names):
        covered = 40 if index < 21 else 5
        files[name] = {"summary": {
            "num_statements": 40, "covered_lines": 40,
            "num_branches": 40, "covered_branches": covered,
            "percent_covered": 100 * (40 + covered) / 80,
        }}
    report = {
        "meta": {"branch_coverage": True},
        "totals": {
            "num_statements": 1000, "covered_lines": 1000,
            "num_branches": 1000, "covered_branches": 860,
            "percent_covered": 93.0,
        },
        "files": files,
    }
    junit = tmp_path / "test-results.xml"
    coverage = tmp_path / "coverage.json"
    ElementTree.ElementTree(suite).write(junit, encoding="unicode")
    coverage.write_text(json.dumps(report), encoding="utf-8")
    return junit, coverage, suite, report


def test_valid_synthetic_reports_pass(tmp_path):
    junit, coverage, _suite, _report = _reports(tmp_path)
    outcome = validate(junit, coverage)
    assert outcome["tests_executed"] == MIN_TEST_CASES - len(EXPECTED_SKIPS)
    assert outcome["branch_only_coverage"] == 86.0
    assert outcome["combined_coverage"] == 93.0


@pytest.mark.parametrize("mutation", [
    "hidden_failure", "reported_error", "new_skip", "skip_removed",
    "orphan_testcase", "unexpected_root", "wrapped_testcase",
    "test_deleted", "duplicate_case", "unnamed_case",
    "falsified_test_count", "falsified_skip_count", "falsified_failure_count",
    "branches_disabled", "branches_not_measured", "inflated_covered_branches",
    "boolean_branch_total", "fractional_branch_total",
    "nan_combined_coverage", "infinite_combined_coverage",
    "boolean_combined_coverage", "overfull_combined_coverage",
    "nan_critical_module", "overfull_critical_module",
    "branch_regression", "combined_regression", "critical_module_missing",
    "critical_module_regression", "missing_modules",
])
def test_quality_gate_rejects_tampered_reports(tmp_path, mutation):
    junit, coverage, suite, report = _reports(tmp_path)
    if mutation == "hidden_failure":
        ElementTree.SubElement(suite[2], "failure", message="mutated failure")
    elif mutation == "orphan_testcase":
        wrapper = ElementTree.Element("testsuites")
        wrapper.append(suite)
        ElementTree.SubElement(
            wrapper, "testcase", classname="backend.tests.test_synthetic",
            name="test_orphan",
        )
        suite = wrapper
    elif mutation == "unexpected_root":
        wrapper = ElementTree.Element("unexpected")
        wrapper.append(suite)
        suite = wrapper
    elif mutation == "wrapped_testcase":
        wrapper = ElementTree.Element("wrapper")
        case = suite[-1]
        suite.remove(case)
        wrapper.append(case)
        suite.append(wrapper)
    elif mutation == "reported_error":
        suite.set("errors", "1")
    elif mutation == "new_skip":
        ElementTree.SubElement(suite[2], "skipped")
    elif mutation == "skip_removed":
        suite[0].remove(suite[0][0])
    elif mutation == "test_deleted":
        suite.remove(suite[-1])
    elif mutation == "duplicate_case":
        suite[-1].set("name", suite[-2].get("name"))
    elif mutation == "unnamed_case":
        del suite[-1].attrib["name"]
    elif mutation == "falsified_test_count":
        suite.set("tests", str(MIN_TEST_CASES + 1))
    elif mutation == "falsified_skip_count":
        suite.set("skipped", "0")
    elif mutation == "falsified_failure_count":
        suite.set("failures", "1")
    elif mutation == "branches_disabled":
        report["meta"]["branch_coverage"] = False
    elif mutation == "branches_not_measured":
        report["totals"]["num_branches"] = 0
    elif mutation == "inflated_covered_branches":
        report["totals"]["covered_branches"] = report["totals"]["num_branches"] + 1
    elif mutation == "boolean_branch_total":
        report["totals"]["num_branches"] = True
    elif mutation == "fractional_branch_total":
        report["totals"]["num_branches"] = 1000.5
    elif mutation == "nan_combined_coverage":
        report["totals"]["percent_covered"] = float("nan")
    elif mutation == "infinite_combined_coverage":
        report["totals"]["percent_covered"] = float("inf")
    elif mutation == "boolean_combined_coverage":
        report["totals"]["percent_covered"] = True
    elif mutation == "overfull_combined_coverage":
        report["totals"]["percent_covered"] = 101.0
    elif mutation == "nan_critical_module":
        report["files"][next(iter(MODULE_MINIMUMS))]["summary"]["percent_covered"] = float("nan")
    elif mutation == "overfull_critical_module":
        report["files"][next(iter(MODULE_MINIMUMS))]["summary"]["percent_covered"] = 101.0
    elif mutation == "branch_regression":
        report["totals"]["covered_branches"] = 10
    elif mutation == "combined_regression":
        report["totals"]["percent_covered"] = 1.0
    elif mutation == "critical_module_missing":
        del report["files"][next(iter(MODULE_MINIMUMS))]
    elif mutation == "critical_module_regression":
        report["files"][next(iter(MODULE_MINIMUMS))]["summary"]["percent_covered"] = 0
    elif mutation == "missing_modules":
        report["files"] = {}
    ElementTree.ElementTree(suite).write(junit, encoding="unicode")
    coverage.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        validate(junit, coverage)


@pytest.mark.parametrize("mutation", [
    "totals_percentage", "totals_counts", "file_percentage", "file_counts",
    "missing_count", "boolean_count", "negative_count", "overfull_count",
    "fractional_count", "extra_file",
])
def test_inconsistent_coverage_counts(tmp_path, mutation):
    junit, coverage, _suite, report = _reports(tmp_path)
    summary = report["files"]["backend/clashbolt/dummy_0.py"]["summary"]
    totals = report["totals"]
    if mutation == "totals_percentage":
        totals["percent_covered"] = 99
    elif mutation == "totals_counts":
        totals["num_statements"] += 1
        totals["percent_covered"] = 100 * 1860 / 2001
    elif mutation == "file_percentage":
        summary["percent_covered"] = 99
    elif mutation == "file_counts":
        summary["covered_lines"] -= 1
        summary["percent_covered"] = 98.75
    elif mutation == "missing_count":
        del summary["covered_lines"]
    elif mutation == "boolean_count":
        summary["covered_lines"] = True
    elif mutation == "negative_count":
        summary["num_statements"] = -1
    elif mutation == "overfull_count":
        summary["covered_lines"] = 41
    elif mutation == "fractional_count":
        summary["num_branches"] = 40.5
    else:
        report["files"]["extra.py"] = {"summary": dict(summary)}
    coverage.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        validate(junit, coverage)


@pytest.mark.parametrize("kind, message", [
    ("branch", "Branch-only coverage"),
    ("combined", "Combined coverage"),
    ("critical", "Critical module"),
])
def test_consistent_reports_still_enforce_floors(tmp_path, kind, message):
    junit, coverage, _suite, report = _reports(tmp_path)
    summaries = [row["summary"] for row in report["files"].values()]
    if kind == "branch":
        for summary in summaries:
            summary["covered_branches"] = 30
    elif kind == "combined":
        for summary in summaries:
            summary["covered_lines"] = 20
    else:
        summaries[0]["covered_lines"] = 0
        summaries[0]["covered_branches"] = 0
    for summary in summaries:
        summary["percent_covered"] = 100 * (
            summary["covered_lines"] + summary["covered_branches"]
        ) / 80
    totals = report["totals"]
    for field in ("covered_lines", "covered_branches"):
        totals[field] = sum(summary[field] for summary in summaries)
    totals["percent_covered"] = 100 * (totals["covered_lines"] + totals["covered_branches"]) / 2000
    coverage.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        validate(junit, coverage)


def _change(workflow, kind):
    jobs = workflow["jobs"]
    if kind == "remove_security":
        del jobs["security"]
    elif kind == "omit_redteam_from_release":
        jobs["release-gate"]["needs"].remove("redteam")
    elif kind == "skip_release_on_failure":
        jobs["release-gate"]["if"] = "${{ success() }}"
    elif kind == "write_permissions":
        workflow["permissions"]["contents"] = "write"
    elif kind == "unpin_checkout":
        jobs["backend"]["steps"][0]["uses"] = "actions/checkout@main"
    elif kind == "skip_backend_on_error":
        jobs["backend"]["steps"][0]["continue-on-error"] = "true"
    elif kind == "drop_browser_coverage":
        jobs["browser"]["steps"] = [
            step for step in jobs["browser"]["steps"]
            if step.get("name") != "Require frontend branch coverage"
        ]
    elif kind == "drop_backend_coverage":
        for step in jobs["backend"]["steps"]:
            if "--cov-fail-under=82" in step.get("run", ""):
                step["run"] = step["run"].replace("--cov-fail-under=82", "--cov-fail-under=1")
                break
    elif kind == "remove_dependency_audit":
        for step in jobs["security"]["steps"]:
            if "pip_audit --strict" in step.get("run", ""):
                step["run"] = step["run"].replace("pip_audit --strict", "pip_audit")
                break
    elif kind == "conditional_redteam":
        jobs["redteam"]["if"] = "${{ false }}"
    elif kind == "remove_native_host_check":
        for step in jobs["backend"]["steps"]:
            if "ci/check-target-service.sh" in step.get("run", ""):
                step["run"] = "echo native host check disabled"
                break
    elif kind in ("remove_bandit", "remove_deployment_audit"):
        needle = (
            "bandit -r backend/clashbolt"
            if kind == "remove_bandit" else "ci/check_deployment_security.py"
        )
        for step in jobs["security"]["steps"]:
            if needle in step.get("run", ""):
                step["run"] = "echo security check removed"
                break
    elif kind == "remove_isolation_mutation_tests":
        for step in jobs["redteam"]["steps"]:
            if "ci/tests/test_deployment_security.py" in step.get("run", ""):
                step["run"] = step["run"].replace(
                    "ci/tests/test_deployment_security.py", ""
                )
                break
    elif kind == "mask_failure":
        jobs["backend"]["steps"][0]["run"] = "echo tests failed || true"
    elif kind == "exclude_tests":
        for step in jobs["backend"]["steps"]:
            if "python -m pytest" in step.get("run", ""):
                step["run"] += " --ignore=backend/tests/test_api.py"
                break
    elif kind == "remove_compatibility":
        del jobs["compatibility"]
    elif kind == "remove_python_311":
        jobs["compatibility"]["strategy"]["matrix"]["python-version"] = ["3.13"]
    elif kind == "remove_package":
        del jobs["package"]
    elif kind == "remove_wheel_smoke":
        for step in jobs["package"]["steps"]:
            if "python -m venv" in step.get("run", ""):
                step["run"] = "echo no wheel smoke"
                break
    elif kind == "ignore_missing_release_artifact":
        for step in jobs["package"]["steps"]:
            if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                step["with"]["if-no-files-found"] = "ignore"
                break
    elif kind == "remove_release_integrity_check":
        for step in jobs["package"]["steps"]:
            if "ci/check_release_artifacts.py" in step.get("run", ""):
                step["run"] = "echo integrity check removed"
                break
    elif kind == "remove_workflow_linter":
        for step in jobs["workflowlint"]["steps"]:
            if "actionlint" in step.get("run", ""):
                step["run"] = "echo lint skipped"
                break
    elif kind == "remove_release_dependency":
        jobs["release-gate"]["needs"].remove("package")
    elif kind == "remove_release_mutation_tests":
        for step in jobs["redteam"]["steps"]:
            if "ci/tests/test_release_artifacts.py" in step.get("run", ""):
                step["run"] = step["run"].replace(
                    "ci/tests/test_release_artifacts.py", ""
                )
                break
    else:
        raise AssertionError(f"Unknown mutation {kind}")


def test_current_workflow_passes_strict_audit():
    audit(WORKFLOW.read_text(encoding="utf-8"), PLAYWRIGHT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", [
    "remove_security", "omit_redteam_from_release", "skip_release_on_failure",
    "write_permissions", "unpin_checkout", "skip_backend_on_error",
    "drop_backend_coverage", "drop_browser_coverage", "remove_dependency_audit",
    "conditional_redteam", "remove_native_host_check",
    "mask_failure", "exclude_tests", "remove_bandit",
    "remove_deployment_audit", "remove_isolation_mutation_tests",
    "remove_compatibility", "remove_python_311", "remove_package",
    "remove_wheel_smoke", "ignore_missing_release_artifact",
    "remove_release_integrity_check", "remove_workflow_linter",
    "remove_release_dependency", "remove_release_mutation_tests",
])
def test_release_guard_rejects_bypass_mutations(kind):
    source = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    modified = copy.deepcopy(source)
    _change(modified, kind)
    with pytest.raises(ValueError):
        audit(yaml.safe_dump(modified), PLAYWRIGHT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("replace", [
    ("forbidOnly: !!process.env.CI", "forbidOnly: false"),
    ("failOnFlakyTests: !!process.env.CI", "failOnFlakyTests: false"),
])
def test_browser_gate_rejects_flaky_and_focused_test_bypasses(replace):
    source = PLAYWRIGHT.read_text(encoding="utf-8")
    assert replace[0] in source
    with pytest.raises(ValueError):
        audit(WORKFLOW.read_text(encoding="utf-8"), source.replace(*replace))
