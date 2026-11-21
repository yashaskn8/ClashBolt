from pathlib import Path


def test_native_units_keep_api_and_worker_separate_and_loopback_only():
    root = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
    api = (root / "clashbolt-api.service").read_text(encoding="utf-8")
    worker = (root / "clashbolt-worker.service").read_text(encoding="utf-8")
    assert "User=clashbolt-api" in api
    assert "User=clashbolt-worker" in worker
    assert "EnvironmentFile=/etc/clashbolt/api.env" in api
    assert "EnvironmentFile=/etc/clashbolt/worker.env" in worker
    assert "clashbolt.api:app --host 127.0.0.1" in api
    assert "python -m clashbolt.worker" in worker
    for unit in (api, worker):
        assert "IPAddressDeny=any" in unit
        assert "IPAddressAllow=localhost" in unit
        assert "NoNewPrivileges=yes" in unit
        assert "ProtectSystem=strict" in unit
        assert "CapabilityBoundingSet=" in unit
        assert "MemoryMax=" in unit
        assert "User=root" not in unit


def test_per_request_target_protects_kernel_and_system_controls():
    root = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
    target = (root / "clashbolt-target@.service").read_text(encoding="utf-8")
    for restriction in (
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "InaccessiblePaths=-/proc -/sys",
        "NoNewPrivileges=yes",
        "MemorySwapMax=0",
    ):
        assert restriction in target
