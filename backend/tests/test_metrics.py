from clashbolt.metrics import operational_metrics


def test_prometheus_metrics_use_only_known_enum_labels():
    text = operational_metrics({
        "runs": {"RUNNING": 3, "UNKNOWN": 1, 'bad"\nmalicious': 4},
        "verifications": {"PENDING": 2},
        "responsive_workers": 1,
        "workers": [{"owner": "worker-secret", "responsive": True}],
    })
    assert 'clashbolt_runs{state="RUNNING"} 3' in text
    assert 'clashbolt_runs{state="UNKNOWN"} 1' in text
    assert 'clashbolt_verification_jobs{state="PENDING"} 2' in text
    assert "clashbolt_responsive_workers 1" in text
    assert "clashbolt_quarantined_agent_plans 0" in text
    assert "worker-secret" not in text
    assert "malicious" not in text
    assert text.endswith("\n")


def test_quarantined_decision_count_has_only_integer_gauge_values():
    output = operational_metrics({
        "runs": {}, "verifications": {}, "workers": [],
        "responsive_workers": 0, "quarantined_agent_plans": 3,
    })
    assert "clashbolt_quarantined_agent_plans 3" in output
    assert "state=\"quarantined\"" not in output
