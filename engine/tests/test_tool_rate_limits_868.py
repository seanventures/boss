# ruff: noqa: F811 — pytest fixtures imported by name and used as arguments
"""BOSSAPP-868: the remaining surface of rate limits on tool calls.

815 already shipped effect=rate_limit (max_calls, window_seconds) counted over tool_call_audit and
deciding deny when spent. This ticket adds, without changing decide()'s semantics:

  * a BUCKET — project (the default; a rule with no bucket counts exactly as 815 did), run or
    agent — so one looping run is stopped while other runs go on;
  * a STRUCTURED REFUSAL on the decision (``refusal`` + ``retry_after_seconds``), raised at the
    receipt door as McpToolRateLimited (an McpToolDenied, so every existing handler still
    answers it), kept on the audit row under screen.policy, and handed to the kernel's agent;
  * per-server/per-tool DEFAULTS: catalogue ``suggested_rules`` offered (never applied) through
    GET /v1/tool-policies/suggestions.

Needs BOSS_TEST_DATABASE_URL naming a disposable test database for the DB tests.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import tool_policy_engine as tpe
from .test_tool_policy_plug_points_815 import (  # noqa: F401 — fixtures
    _audits, _call, _gateway_verdict, _kernel, _rule, eng, world)


def _ctx(**kw):
    return tpe.PolicyContext(server="s", tool="t", project=None, **kw)


# ── pure ─────────────────────────────────────────────────────────────────────────────────────

def test_bucket_is_validated_and_absent_by_default():
    plain = tpe.validate({"effect": "rate_limit",
                          "rate_limit": {"max_calls": 5, "window_seconds": 60}})
    assert plain["rate_limit"] == {"max_calls": 5, "window_seconds": 60}  # byte-identical to 815
    run = tpe.validate({"effect": "rate_limit",
                        "rate_limit": {"max_calls": 5, "window_seconds": 60, "bucket": "run"}})
    assert run["rate_limit"]["bucket"] == "run"
    with pytest.raises(tpe.ToolPolicyError, match="bucket"):
        tpe.validate({"effect": "rate_limit",
                      "rate_limit": {"max_calls": 5, "window_seconds": 60, "bucket": "tenant"}})


def test_no_connection_refusal_falls_back_to_the_window(monkeypatch):
    monkeypatch.setattr(tpe, "_rate_spent", lambda c, r, x: (True, 2, 2))
    rule = {"id": "r1", "scope": "project", "priority": 1, "effect": "rate_limit",
            "selector": {}, "conditions": [], "status": "active",
            "rate_limit": {"max_calls": 2, "window_seconds": 90, "bucket": "agent"}}
    out = tpe.decide(_ctx(role="forge"), rules=[rule])
    assert out["decision"] == "deny" and out["reason"] == "rate_limited"
    assert out["refusal"] == {"code": "rate_limited", "rule_id": "r1", "server": "s", "tool": "t",
                              "max_calls": 2, "window_seconds": 90, "bucket": "agent",
                              "retry_after_seconds": 90, "bucket_key": "forge"}
    assert out["retry_after_seconds"] == 90 and "per agent" in out["explanation"]


def test_a_decision_that_is_not_a_spent_limit_carries_no_refusal():
    rule = {"id": "r1", "scope": "project", "priority": 1, "effect": "deny", "selector": {},
            "conditions": [], "status": "active"}
    out = tpe.decide(_ctx(), rules=[rule])
    assert out["decision"] == "deny" and "refusal" not in out
    assert "refusal" not in tpe.decide(_ctx(), rules=[])


# ── against the database ─────────────────────────────────────────────────────────────────────

def _call_as(w, tool, args, *, run, role="forge"):
    from boss_engine.services.mcp_registry import McpServerError, call_tool_with_receipt
    import uuid

    from .test_tool_policy_plug_points_815 import _server

    refused = None
    with w.eng.begin() as c:
        try:
            return call_tool_with_receipt(
                c, w.settings, w.pid, _server(w), tool, args or {},
                dedupe_key=f"t868:{uuid.uuid4().hex}", tenant_scope=("org_815", "ws_815"),
                approval_about={"run_id": run}, policy_context={"role_key": role, "run_id": run})
        except McpServerError as error:
            refused = error
    raise refused


def test_a_spent_limit_refuses_with_a_structured_refusal_and_an_audit_row(world):
    from boss_engine.services.mcp_registry import McpToolDenied, McpToolRateLimited

    w = world
    rl = _rule(w, effect="rate_limit", selector={"tool": "echo"},
               rate_limit={"max_calls": 2, "window_seconds": 3600})
    _call(w, "echo", {"text": "1"})
    _call(w, "echo", {"text": "2"})
    with pytest.raises(McpToolRateLimited) as refused:
        _call(w, "echo", {"text": "3"})
    assert isinstance(refused.value, McpToolDenied)  # every existing handler still answers it
    refusal = refused.value.refusal
    assert refusal["code"] == "rate_limited" and refusal["rule_id"] == rl["id"]
    assert refusal["bucket"] == "project" and refusal["max_calls"] == 2
    assert 3500 <= refusal["retry_after_seconds"] <= 3600
    last = _audits(w, "echo")[-1]
    assert last["outcome"] == "refused" and last["decision"] == "deny"
    assert last["matched_policy_ids"] == [rl["id"]]
    assert last["screen"]["policy"]["reason"] == "rate_limited"
    assert last["screen"]["policy"]["refusal"]["rule_id"] == rl["id"]
    # the gateway's verdict reads the same decision
    verdict = _gateway_verdict(w, "echo")
    assert verdict.decision == "deny" and verdict.reason == "rate_limited"


def test_a_run_bucket_stops_one_run_and_lets_another_go_on(world):
    from boss_engine.services.mcp_registry import McpToolRateLimited

    w = world
    _rule(w, effect="rate_limit", selector={"tool": "echo"},
          rate_limit={"max_calls": 1, "window_seconds": 3600, "bucket": "run"})
    _call_as(w, "echo", {"text": "a"}, run="run_a")
    with pytest.raises(McpToolRateLimited) as refused:
        _call_as(w, "echo", {"text": "a2"}, run="run_a")
    assert refused.value.refusal["bucket"] == "run"
    assert refused.value.refusal["bucket_key"] == "run_a"
    assert _call_as(w, "echo", {"text": "b"}, run="run_b")["output"]  # another run goes on


def test_an_agent_bucket_counts_per_agent(world):
    from boss_engine.services.mcp_registry import McpToolRateLimited

    w = world
    _rule(w, effect="rate_limit", selector={"tool": "echo"},
          rate_limit={"max_calls": 1, "window_seconds": 3600, "bucket": "agent"})
    _call_as(w, "echo", {"text": "a"}, run="r1", role="forge")
    with pytest.raises(McpToolRateLimited):
        _call_as(w, "echo", {"text": "a"}, run="r2", role="forge")
    assert _call_as(w, "echo", {"text": "b"}, run="r3", role="scout")["output"]


def test_the_kernel_hands_the_agent_the_refusal(world):
    w = world
    _rule(w, effect="rate_limit", selector={"server": "boss", "tool": "list_things"},
          rate_limit={"max_calls": 0, "window_seconds": 60})
    result, reg, _ = _kernel(w, "list_things")
    assert reg.ran == [] and result.status == "refused"
    assert result.content["reason"] == "rate_limited"
    assert result.content["refusal"]["code"] == "rate_limited"
    assert result.content["retry_after_seconds"] == 60
    assert result.content["error"] == "denied_by_policy"  # unchanged for existing readers


def test_catalogue_suggestions_are_offered_not_applied(world):
    from boss_engine.services.kit_connections import catalogue_entry

    w = world
    with w.eng.begin() as c:
        c.execute(text("UPDATE mcp_servers SET provenance = provenance || "
                       "'{\"catalogue\": \"github\"}'::jsonb WHERE id = :id"), {"id": w.sid})
        before = tpe.list_rules(c, w.pid)
        found = tpe.suggested_rules(c, w.pid)
    assert before == []  # nothing applied by itself
    entry = catalogue_entry("github")
    assert entry and entry.get("suggested_rules")
    assert found and found[0]["catalogue"] == "github" and found[0]["server"] == w.name
    body = found[0]["rule"]
    assert body["effect"] == "rate_limit" and body["rate_limit"]["bucket"] == "run"
    assert body["selector"] == {"server": w.name, "tool": "create_*"}
    # adding it as offered makes the suggestion go away
    _rule(w, **body)
    with w.eng.begin() as c:
        assert tpe.suggested_rules(c, w.pid) == []


def test_the_suggestions_route(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.tool_policies import build_tool_policies_router

    w = world
    app = FastAPI()
    app.include_router(build_tool_policies_router(
        engine=w.eng, require_reader=lambda c, p: None,
        require_write_actor=lambda c, p, a, k: "ceo"))
    res = TestClient(app).get("/v1/tool-policies/suggestions", params={"project_id": w.pid})
    assert res.status_code == 200
    assert res.json()["buckets"] == ["project", "run", "agent"]
    assert isinstance(res.json()["suggestions"], list)
