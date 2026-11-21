"""Ungated measurements stay separate from approved controlled witnesses."""

import json
import os
import subprocess
import sys

import psycopg
import pytest

from clashbolt.ungated import measure_ungated, sample_ungated


@pytest.mark.parametrize("variant", ["racy", "locked"])
def test_ungated_fixture_uses_fresh_native_databases_without_false_claims(variant):
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not control or not admin:
        pytest.skip("Separate disposable native PostgreSQL clusters required")
    with psycopg.connect(admin) as conn:
        before = {
            row[0] for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'cb_target_%'"
            )
        }
    report = measure_ungated(admin, control, variant=variant, trials=2)
    assert report["benchmark"] == "UNGATED_NATIVE_TWO_CLIENTS"
    assert report["trials"] == 2
    assert report["pass_count"] + report["fail_count"] == 2
    assert report["unknown_count"] == 0
    assert report["reproduction_claim"] == "OBSERVED_ONLY_NOT_GUARANTEED"
    assert report["general_target_supported"] is False
    if variant == "locked":
        assert report["pass_count"] == 2
    for sample in report["samples"]:
        assert sample["verdict"] in ("PASS", "FAIL")
        assert sample["controlled_schedule"] is False
        assert sample["independent_target_proven"] is False
        assert sample["scope"] == "SHIPPED_SYNTHETIC_FIXTURE_ONLY"
        assert len(sample["client_observations"]) == 2
        assert {r["client"] for r in sample["client_observations"]} == {"a", "b"}
        assert sample["final_state"]["initial_stock"] == 1
        assert "requested_gates" not in sample
        assert "evidence_sha256" not in sample
        assert "url" not in json.dumps(sample)
    with psycopg.connect(admin) as conn:
        after = {
            row[0] for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'cb_target_%'"
            )
        }
    assert after == before


def test_ungated_rejects_unsupported_variant_or_unbounded_trial_count():
    with pytest.raises(ValueError, match="shipped"):
        sample_ungated("not-even-a-connection", "external")
    for trials in (0, 21, True, 2.1):
        with pytest.raises(ValueError, match="1-20"):
            measure_ungated("unused", "unused", variant="racy", trials=trials)
    with pytest.raises(ValueError, match="1-20"):
        measure_ungated("unused", "unused", variant="unknown", trials=2)


def test_ungated_cli_is_reachable_and_does_not_print_target_credentials():
    control = os.getenv("CLASHBOLT_TEST_DATABASE_URL")
    admin = os.getenv("CLASHBOLT_TEST_TARGET_ADMIN_URL")
    if not control or not admin:
        pytest.skip("Separate disposable native PostgreSQL clusters required")
    result = subprocess.run(
        [sys.executable, "-m", "clashbolt.ungated", "--variant", "locked",
         "--trials", "1"],
        env={
            **os.environ,
            "CLASHBOLT_TARGET_ADMIN_URL": admin,
            "CLASHBOLT_DATABASE_URL": control,
        },
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["pass_count"] == 1
    assert "password=" not in result.stdout.lower()
    assert "postgresql://" not in result.stdout.lower()
