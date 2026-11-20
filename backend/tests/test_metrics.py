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
    assert "worker-secret" not in text
    assert "malicious" not in text
    assert text.endswith("\n")
