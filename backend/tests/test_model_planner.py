import asyncio
import json

import httpx
import pytest
from clashbolt.model_planner import MODEL_URL, propose_model_plan


def test_valid_model_suggestion_cannot_change_authority(monkeypatch):
    monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "test-fixture-model")
    captured = []

    def responder(request: httpx.Request):
        assert str(request.url) == MODEL_URL
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "response": json.dumps({
                    "schedule": "serial",
                    "conflict_edge": "shared.inventory",
                    "reason": "Explore a completely serial baseline gate order",
                    "information_gain": 0.4,
                }),
                "eval_count": 12,
            },
        )

    plan = asyncio.run(
        propose_model_plan("racy", transport=httpx.MockTransport(responder))
    )
    assert captured[0]["stream"] is False
    assert captured[0]["options"]["num_predict"] == 256
    assert plan["schedule"] == "serial"
    assert plan["planner_kind"] == "LOCAL_MODEL_UNPROMOTED"
    assert plan["target_id"] == "synthetic-inventory"
    assert plan["max_steps"] == 4
    assert plan["stop_after"] == 1
    assert plan["model_tokens_used"] == 12


def test_prompt_injection_cannot_expand_allowed_tool_scope(monkeypatch):
    monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "test-fixture-model")
    malicious = {
        "schedule": "overlap",
        "conflict_edge": "shared.inventory",
        "reason": "Execute an arbitrary external command instead of the inventory target",
        "information_gain": 1.0,
        "tool_command": "external-script",
    }
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"response": json.dumps(malicious)})
    )
    plan = asyncio.run(propose_model_plan("racy", transport=transport))
    assert plan["planner_kind"] == "DETERMINISTIC_FALLBACK"
    assert plan["schedule"] == "overlap"
    assert "tool_command" not in plan


def test_model_outage_and_invalid_token_usage_fall_back(monkeypatch):
    monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "test-fixture-model")
    failed = httpx.MockTransport(lambda _: httpx.Response(503))
    plan = asyncio.run(propose_model_plan("locked", transport=failed))
    assert plan["planner_kind"] == "DETERMINISTIC_FALLBACK"
    bloated = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "response": json.dumps({
                    "schedule": "serial",
                    "conflict_edge": "shared.inventory",
                    "reason": "Consider an ordinary serial execution sequence",
                    "information_gain": 0.1,
                }),
                "eval_count": 9999,
            },
        )
    )
    plan = asyncio.run(propose_model_plan("racy", transport=bloated))
    assert plan["planner_kind"] == "DETERMINISTIC_FALLBACK"


def test_model_without_configuration_never_calls_http(monkeypatch):
    monkeypatch.delenv("CLASHBOLT_LOCAL_MODEL", raising=False)
    plan = asyncio.run(propose_model_plan("racy"))
    assert plan["model_status"] == "NOT_CONFIGURED"


@pytest.mark.parametrize("body", [None, {}, {"response": []}, {"response": "x" * 50_000}])
def test_invalid_model_envelope_never_becomes_an_authoritative_plan(monkeypatch, body):
    monkeypatch.setenv("CLASHBOLT_LOCAL_MODEL", "test-fixture-model")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    plan = asyncio.run(propose_model_plan("locked", transport=transport))
    assert plan["planner_kind"] == "DETERMINISTIC_FALLBACK"
    assert plan["model_status"] == "UNAVAILABLE_OR_INVALID"
    assert plan["max_steps"] == 4
