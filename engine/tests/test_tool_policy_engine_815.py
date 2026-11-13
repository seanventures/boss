"""BOSSAPP-815: the ordered tool-policy engine's ONE decision, as a table.

Pure: rules are handed to decide() as data, no database. Pinned here:

  * ORDER — priority first (lower runs first), then the more specific scope (workflow > agent >
    project > org), then the stricter effect, then age; the first terminal match decides;
  * SCOPES — agent:<role> and workflow:<id> apply only to that caller;
  * SELECTORS — server / tool globs, the server matched by name or id;
  * CONDITIONS — equals / prefix / regex over a JSONPath-lite path, ALL must hold, and the
    language is TOTAL (a missing path, wrong type or bad pattern is simply False);
  * THE CEILINGS — Off stays Off, a tier >= 2 tool never comes out allow, a quarantined tool
    asks, an unreviewed server asks, whatever a rule says;
  * expiry, disabled rules, schema pins and rate limits;
  * the result names the rules it matched and says why.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import tool_policy_engine as tpe
from boss_engine.services.tool_policy_engine import PolicyContext, decide


def ctx(**over) -> PolicyContext:
    base = dict(server="docs", tool="read_file", arguments={"path": "/srv/app/a.txt", "n": 3},
                tier=1, project="p1", role="forge", server_id="mcp_docs", base="allow")
    base.update(over)
    return PolicyContext(**base)


def rule(rid, effect="allow", *, priority=100, scope="project", server="*", tool="*",
         conditions=None, **extra):
    return {"id": rid, "effect": effect, "priority": priority, "scope": scope,
            "selector": {"server": server, "tool": tool}, "conditions": conditions or [],
            "status": "active", "source": "operator", **extra}


# ── no rules: the registry's own policy stands ───────────────────────────────────────────────

@pytest.mark.parametrize("base,decision", [("allow", "allow"), ("ask", "ask"), ("off", "deny")])
def test_no_rule_leaves_the_base_policy(base, decision):
    out = decide(ctx(base=base), rules=[])
    assert out["decision"] == decision
    assert out["rule_id"] is None
    assert out["matched_policy_ids"] == (["guard:off"] if base == "off" else [f"base:{base}"])
    assert out["explanation"].startswith(decision.upper())


# ── order ────────────────────────────────────────────────────────────────────────────────────

def test_lower_priority_number_decides_first():
    rules = [rule("r_allow", "allow", priority=50), rule("r_deny", "deny", priority=10)]
    out = decide(ctx(), rules=rules)
    assert out["decision"] == "deny" and out["rule_id"] == "r_deny"
    rules = [rule("r_allow", "allow", priority=5), rule("r_deny", "deny", priority=10)]
    assert decide(ctx(base="ask"), rules=rules)["rule_id"] == "r_allow"


def test_equal_priority_more_specific_scope_wins_then_stricter_effect():
    rules = [rule("r_proj", "deny", scope="project"), rule("r_agent", "allow", scope="agent:forge"),
             rule("r_wf", "ask", scope="workflow:wf1")]
    assert decide(ctx(base="ask"), rules=rules)["rule_id"] == "r_agent"
    assert decide(ctx(base="ask", workflow="wf1"), rules=rules)["rule_id"] == "r_wf"
    same = [rule("a_allow", "allow"), rule("b_deny", "deny")]
    assert decide(ctx(), rules=same)["rule_id"] == "b_deny"


def test_order_is_total_and_input_order_independent():
    rules = [rule("r2", "ask"), rule("r1", "ask"), rule("r3", "deny", priority=100)]
    a = decide(ctx(), rules=rules)
    b = decide(ctx(), rules=list(reversed(rules)))
    assert a == b and a["rule_id"] == "r3"


# ── scopes ───────────────────────────────────────────────────────────────────────────────────

def test_agent_and_workflow_scopes_apply_only_to_that_caller():
    r = [rule("r_a", "deny", scope="agent:forge"), rule("r_w", "deny", scope="workflow:nightly")]
    assert decide(ctx(role="forge"), rules=r)["rule_id"] == "r_a"
    assert decide(ctx(role="scout"), rules=r)["rule_id"] is None
    assert decide(ctx(role=None), rules=r)["rule_id"] is None
    assert decide(ctx(role="scout", workflow="nightly"), rules=r)["rule_id"] == "r_w"


# ── selectors ────────────────────────────────────────────────────────────────────────────────

def test_selector_globs_on_server_name_or_id_and_tool():
    r = [rule("r1", "deny", server="doc*", tool="read_*")]
    assert decide(ctx(), rules=r)["decision"] == "deny"
    r = [rule("r1", "deny", server="mcp_docs", tool="read_file")]
    assert decide(ctx(), rules=r)["decision"] == "deny"
    r = [rule("r1", "deny", server="other", tool="*")]
    assert decide(ctx(), rules=r)["decision"] == "allow"
    r = [rule("r1", "deny", tool="write_*")]
    assert decide(ctx(), rules=r)["decision"] == "allow"


# ── conditions ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cond,holds", [
    ({"path": "$.path", "op": "prefix", "value": "/srv/"}, True),
    ({"path": "$.path", "op": "prefix", "value": "/etc/"}, False),
    ({"path": "$.path", "op": "regex", "value": r"\.txt$"}, True),
    ({"path": "$.path", "op": "regex", "value": r"^/etc"}, False),
    ({"path": "$.n", "op": "equals", "value": 3}, True),
    ({"path": "$.n", "op": "equals", "value": "3"}, False),
    ({"path": "n", "op": "equals", "value": 3}, True),
    ({"path": "$.items[1].name", "op": "equals", "value": "b"}, True),
    ({"path": "$.items.1.name", "op": "equals", "value": "b"}, True),
    ({"path": '$["odd key"]', "op": "equals", "value": True}, True),
    ({"path": "$.n", "op": "prefix", "value": "3"}, True),        # numbers read as text
    ({"path": "$.missing", "op": "equals", "value": None}, False),  # missing is not null
    ({"path": "$.items", "op": "prefix", "value": "a"}, False),     # a list is not text
    ({"path": "$.path", "op": "regex", "value": "("}, False),      # bad pattern: False, no raise
    ({"path": "$..", "op": "equals", "value": 1}, False),          # not a path
    ({"path": "$.path", "op": "contains", "value": "srv"}, False), # not an operator
    ({"path": None, "op": "equals", "value": 1}, False),
    ({"path": "$.items[9]", "op": "equals", "value": 1}, False),
])
def test_conditions_are_total(cond, holds):
    args = {"path": "/srv/app/a.txt", "n": 3, "items": [{"name": "a"}, {"name": "b"}],
            "odd key": True}
    assert tpe.condition_holds(cond, args) is holds


def test_every_condition_must_hold():
    r = [rule("r1", "deny", conditions=[{"path": "$.path", "op": "prefix", "value": "/srv"},
                                        {"path": "$.n", "op": "equals", "value": 4}])]
    assert decide(ctx(), rules=r)["rule_id"] is None
    r[0]["conditions"][1]["value"] = 3
    assert decide(ctx(), rules=r)["rule_id"] == "r1"


def test_validation_refuses_what_the_language_cannot_say():
    for bad in ([{"path": "$..x", "op": "equals", "value": 1}],
                [{"path": "$.x", "op": "contains", "value": "a"}],
                [{"path": "$.x", "op": "regex", "value": "("}],
                [{"path": "$.x", "op": "prefix", "value": ""}],
                [{"path": "$.x", "op": "equals", "value": 1}] * 9):
        with pytest.raises(tpe.ToolPolicyError):
            tpe.validate({"effect": "deny", "conditions": bad})
    with pytest.raises(tpe.ToolPolicyError):
        tpe.validate({"effect": "maybe"})
    with pytest.raises(tpe.ToolPolicyError):
        tpe.validate({"effect": "deny", "scope": "agent:"})
    with pytest.raises(tpe.ToolPolicyError):
        tpe.validate({"effect": "rate_limit"})
    ok = tpe.validate({"effect": "rate_limit", "rate_limit": {"max_calls": 5,
                                                               "window_seconds": 60}})
    assert ok["rate_limit"] == {"max_calls": 5, "window_seconds": 60}
    assert ok["selector"] == {"server": "*", "tool": "*"} and ok["scope"] == "project"


# ── the ceilings ─────────────────────────────────────────────────────────────────────────────

def test_off_stays_off_whatever_a_rule_says():
    out = decide(ctx(base="off"), rules=[rule("r1", "allow", priority=-1000)])
    assert out["decision"] == "deny" and out["matched_policy_ids"] == ["guard:off"]
    assert out["reason"] == "off"


def test_tier_floor_is_a_hard_ceiling():
    out = decide(ctx(tier=2, base="ask"), rules=[rule("r1", "allow")])
    assert out["decision"] == "ask"
    assert out["matched_policy_ids"] == ["r1", "guard:tier_floor"]
    assert "always asks" in out["explanation"]
    # a rule can still make it stricter
    assert decide(ctx(tier=2, base="ask"), rules=[rule("r1", "deny")])["decision"] == "deny"


def test_quarantine_and_unreviewed_stay_ask():
    out = decide(ctx(base="ask", quarantined=True), rules=[rule("r1", "allow")])
    assert out["decision"] == "ask" and out["matched_policy_ids"][-1] == "guard:quarantine"
    out = decide(ctx(unreviewed=True), rules=[rule("r1", "allow")])
    assert out["decision"] == "ask" and out["matched_policy_ids"][-1] == "guard:unreviewed"
    assert out["reason"] == "unreviewed"
    out = decide(ctx(unreviewed=True), rules=[])
    assert out["decision"] == "ask" and out["matched_policy_ids"] == ["base:allow",
                                                                      "guard:unreviewed"]


def test_an_allow_rule_lifts_a_tier_one_ask():
    out = decide(ctx(base="ask"), rules=[rule("r1", "allow")])
    assert out["decision"] == "allow" and out["matched_policy_ids"] == ["r1"]


# ── status, expiry, pins ─────────────────────────────────────────────────────────────────────

def test_disabled_revoked_and_expired_rules_are_skipped():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    rules = [rule("r_dis", "deny", status="disabled"), rule("r_rev", "deny", status="revoked"),
             rule("r_old", "deny", expires_at=now - timedelta(seconds=1)),
             rule("r_bad", "deny", expires_at="not a date")]
    assert decide(ctx(), rules=rules, now=now)["rule_id"] is None
    rules.append(rule("r_live", "deny", expires_at=(now + timedelta(hours=1)).isoformat()))
    assert decide(ctx(), rules=rules, now=now)["rule_id"] == "r_live"


def test_a_pinned_rule_never_applies_to_a_changed_tool():
    r = [rule("r1", "allow", schema_sha256="aaa", source="decision")]
    out = decide(ctx(base="ask", schema_sha256="bbb"), rules=r)
    assert out["decision"] == "ask" and out["rule_id"] is None and out["stale"] == ["r1"]
    assert "older definition" in out["explanation"]
    assert decide(ctx(base="ask", schema_sha256="aaa"), rules=r)["decision"] == "allow"


# ── rate limits ──────────────────────────────────────────────────────────────────────────────

def test_rate_limit_without_a_count_is_noted_and_passes_through():
    r = [rule("r_rl", "rate_limit", rate_limit={"max_calls": 2, "window_seconds": 60}),
         rule("r_ask", "ask", priority=200)]
    out = decide(ctx(), rules=r)
    assert out["decision"] == "ask"
    assert out["matched_policy_ids"] == ["r_rl", "r_ask"]
    assert "has room" in out["explanation"]


def test_rate_limit_spent_denies(monkeypatch):
    monkeypatch.setattr(tpe, "_rate_spent", lambda c, r, x: (True, 2, 2))
    r = [rule("r_rl", "rate_limit", rate_limit={"max_calls": 2, "window_seconds": 60})]
    out = decide(ctx(), rules=r)
    assert out["decision"] == "deny" and out["reason"] == "rate_limited"
    assert out["rule_id"] == "r_rl" and "spent" in out["explanation"]


# ── the explanation and ids ──────────────────────────────────────────────────────────────────

def test_the_result_names_the_rule_and_says_why():
    r = [rule("r1", "deny", tool="read_*",
              conditions=[{"path": "$.path", "op": "prefix", "value": "/srv"}])]
    out = decide(ctx(), rules=r)
    assert set(out) >= {"decision", "matched_policy_ids", "explanation", "rule_id"}
    assert "r1" in out["explanation"] and "read_* on *" in out["explanation"]
    assert "$.path prefix \"/srv\"" in out["explanation"]


def test_gateway_verdict_keeps_its_shape_and_reasons():
    from boss_engine.services import mcp_gateway

    server = {"id": "s", "name": "docs", "risk_tier": 1, "provenance": {"trusted": True},
              "tools": [{"name": "read"}]}
    v = mcp_gateway.tool_policy_verdict(server, "read")
    assert v == mcp_gateway.Verdict("allow", "allow")
    assert v.matched_policy_ids == ("base:allow",) and v.explanation.startswith("ALLOW")
    assert mcp_gateway.tool_policy_verdict(server, "read", granted_tools=["x"]) == \
        mcp_gateway.Verdict("deny", "not_granted")
