"""Audit the GitHub Actions release policy as data; fail if a gate is weakened.

The workflow can be audited without running third-party code or touching an
external target. This guard also runs negative mutation tests in CI.
"""

import re
import sys
from pathlib import Path

import yaml

REQUIRED = {"backend", "frontend", "browser", "security", "redteam"}
COMMANDS = {
    "backend": (
        "python -m ruff check .",
        "vulture backend/clashbolt",
        "--cov-branch",
        "--cov-fail-under=82",
        "--junitxml=",
        "--cov-report=json:",
        "ci/check_quality_report.py",
        "ci/check-control-crash-recovery.py",
        "ci/check-target-service.sh",
    ),
    "frontend": ("npm ci", "npm run build"),
    "browser": (
        "playwright install",
        "CLASHBOLT_BROWSER_MODE=static",
        "CLASHBOLT_BROWSER_MODE=accounts",
        "playwright test",
    ),
    "security": ("pip_audit --strict", "npm audit --audit-level=high"),
    "redteam": (
        "ci/check_ci_policy.py",
        "ci/tests/test_gate_validation.py",
        "test_adversarial_fixture.py",
        "--timeout=30",
    ),
}
PIN = re.compile(r"^[^\s@]+@[a-f0-9]{40}$")


def audit(workflow: str, playwright: str) -> None:
    document = yaml.load(workflow, Loader=yaml.BaseLoader)
    if not isinstance(document, dict):
        raise ValueError("Workflow must be a YAML mapping")
    events = document.get("on", {})
    if not isinstance(events, dict) or not {
        "push", "pull_request", "schedule", "workflow_dispatch",
    }.issubset(events):
        raise ValueError("Required push, PR, scheduled and manual CI triggers missing")
    if events["push"].get("branches") != ["main"]:
        raise ValueError("Main-branch push verification missing")
    if document.get("permissions") != {"contents": "read"}:
        raise ValueError("CI must use read-only repository permissions")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != REQUIRED | {"release-gate"}:
        raise ValueError("Every release gate and red-team job must be mandatory")
    if document.get("concurrency", {}).get("cancel-in-progress") != "true":
        raise ValueError("Overlapping same-ref CI runs must be cancelled")

    for name, job in jobs.items():
        if not isinstance(job, dict):
            raise ValueError(f"Invalid job: {name}")
        timeout = int(job.get("timeout-minutes", "0"))
        if not 1 <= timeout <= 30:
            raise ValueError(f"Missing or unreasonable timeout: {name}")
        if job.get("permissions", {"contents": "read"}) != {"contents": "read"}:
            raise ValueError(f"Job has elevated permissions: {name}")
        if name == "release-gate":
            if job.get("if") != "${{ always() }}":
                raise ValueError("Release gate must run even when upstream fails")
            if set(job.get("needs", [])) != REQUIRED:
                raise ValueError("Release gate missing required dependencies")
        elif job.get("if") is not None or job.get("continue-on-error") is not None:
            raise ValueError(f"Critical job can be conditionally skipped: {name}")
        steps = job.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"Job has no runnable steps: {name}")
        commands = "\n".join(
            str(step.get("run", "")) for step in steps if isinstance(step, dict)
        )
        for token in COMMANDS.get(name, ()):
            if token not in commands:
                raise ValueError(f"Missing {name} security requirement: {token}")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError(f"Malformed step in {name}")
            if "continue-on-error" in step:
                raise ValueError("No CI step may downgrade a failure")
            command = str(step.get("run", ""))
            if any(
                bypass in command
                for bypass in (
                    "|| true", "set +e", "--ignore=", "--deselect=",
                    "--ignore-glob=", "--maxfail=", "--last-failed",
                )
            ):
                raise ValueError("CI command attempts to mask errors or omit tests")
            condition = step.get("if")
            if condition is not None and not (
                name == "browser"
                and step.get("name", "").startswith("Diagnose native worker")
                and condition == "${{ failure() }}"
            ):
                raise ValueError("Mandatory CI step has a bypass condition")
            action = step.get("uses")
            if action:
                if not PIN.fullmatch(action):
                    raise ValueError(f"Action must be pinned to full SHA: {action}")
                if action.startswith("actions/checkout@"):
                    options = step.get("with", {})
                    if options.get("persist-credentials") != "false":
                        raise ValueError("Checkout credentials must not persist")
    gate = jobs["release-gate"]
    commands = "\n".join(str(x.get("run", "")) for x in gate["steps"])
    for item in ("BACKEND", "FRONTEND", "BROWSER", "SECURITY", "REDTEAM"):
        expected = "${{ needs." + item.lower() + ".result }}"
        if expected not in str(gate["steps"][0].get("env", {}).get(item, "")):
            raise ValueError(f"Missing release dependency status: {item}")
    if 'if [[ "$result" != "success" ]]' not in commands:
        raise ValueError("Release gate must explicitly reject non-success results")
    if not re.search(r"forbidOnly:\s*!!process\.env\.CI", playwright):
        raise ValueError("Playwright must reject accidental test.only in CI")
    if not re.search(r"failOnFlakyTests:\s*!!process\.env\.CI", playwright):
        raise ValueError("Flaky browser reruns must fail CI")


def main() -> int:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    playwright = Path("frontend/playwright.config.ts").read_text(encoding="utf-8")
    audit(workflow, playwright)
    print("CI policy verified: all mandatory jobs, pinned actions and fail-closed gates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
