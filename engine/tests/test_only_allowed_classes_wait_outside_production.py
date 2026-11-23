"""P0: OUTSIDE PRODUCTION, BOSS ASKS A PERSON ONLY FOR WHAT A PERSON MUST DECIDE (BOSSAPP-1441).

The operator, 2026-11-23, the third time they said it: "Let's offer more independence for BOSS
agents please, I dont like everytime asking me to do stuff.. it is unnecessary ask, that too for
development, it makes sense for PROD deployment and other fixes that release to PROD. But for
local development I dont need to validate just make the output.. then human can validate it."
And earlier: "BOSS agents are supposed to fix autonomously without help unattended."

So a card may wait for a person outside production only when it is:
  * ask_credential   — a real credential a person types
  * ask_spend        — spend beyond the budget a person set
  * explicit_stop    — a stop a PERSON set ("Review the plan first", a tool set to ask,...)
  * ask_prod_release — a release to production or the public by construction
ADDING A CLASS TO THAT SET MEANS EDITING THIS TEST, next to the operator's words.

This reads every card BOSS writes (the producer scan of
test_every_decision_boss_raises_has_its_buttons_decided) and fails when one has no autonomy
class, or when its class would make it wait outside production. It also pins: every
auto-decision is named and has an undo kind (or says why it has none); production still asks for
a deploy, a merge into a branch that ships to production, and production infrastructure; and a
workflow Deploy step's gate follows its resolved target.
"""
from __future__ import annotations

import pytest

from boss_engine.services import (autonomy_gate, autonomy_policy, decision_actions, production,
                                  workflow_capabilities, workflow_catalogue)

from . import test_every_decision_boss_raises_has_its_buttons_decided as scan

PINNED_ALLOWED_DEV_WAITS = frozenset({"ask_credential", "ask_spend", "explicit_stop",
                                      "ask_prod_release"})


def test_the_allowed_waits_are_exactly_the_operators_four_plus_explicit_stops():
    assert autonomy_gate.ALLOWED_DEV_WAITS == PINNED_ALLOWED_DEV_WAITS


def _produced_actions() -> set[str]:
    out = set()
    for found in scan.ALL_FINDINGS:
        if found.action:
            out.add(found.action)
        elif found.prefix:
            out.add(found.prefix + "x")
    return out


def test_every_card_boss_writes_has_an_autonomy_class():
    missing = sorted(a for a in _produced_actions() | set(decision_actions.KNOWN_ACTIONS)
                     if autonomy_gate.rule_for({"recommendation": {"action": a}}) is None)
    assert not missing, ("Give each a class in autonomy_gate.ACTIONS (act / act_capped / notice "
                         "/ ask_prod_only, or one of the allowed waits): " + ", ".join(missing))


def test_every_gate_capability_has_a_class():
    gates = [c.key for c in workflow_capabilities.CAPABILITIES.values()
             if c.is_gate or c.risk_tier >= workflow_capabilities.MANUAL_APPROVAL_RISK_FLOOR]
    for key in gates:
        rule = autonomy_gate.rule_for({"recommendation": {"capability": key}})
        assert rule is not None, key
        assert rule.cls in autonomy_gate.ACTS_IN_DEV | autonomy_gate.ALLOWED_DEV_WAITS, key


@pytest.mark.parametrize("action", sorted(autonomy_gate.ACTIONS))
def test_no_class_waits_outside_production_unless_it_is_allowed(action):
    rule = autonomy_gate.ACTIONS[action]
    assert rule.cls in autonomy_gate.ACTS_IN_DEV | autonomy_gate.ALLOWED_DEV_WAITS, (
        f"{action}: class {rule.cls!r} would wait for a person outside production")


def _dev_world(monkeypatch, *, prod: bool):
    monkeypatch.setattr(autonomy_policy, "load", lambda *a, **k: autonomy_policy.DEFAULT)
    verdict = production.Verdict(prod, True, "prod" if prod else "staging", "test", "test")
    monkeypatch.setattr(production, "resolve", lambda *a, **k: verdict)


@pytest.mark.parametrize("action", sorted(autonomy_gate.ACTIONS))
def test_outside_production_a_card_acts_or_waits_for_an_allowed_reason(action, monkeypatch):
    _dev_world(monkeypatch, prod=False)
    found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 2, "recommendation": {
        "action": action, "target_id": "t", "pr_base": "rel/x", "repo": "o/r",
        "amount_usd": 0.5}})
    assert found.acts or found.cls in autonomy_gate.ALLOWED_DEV_WAITS, (action, found)


@pytest.mark.parametrize("action", sorted(a for a, r in autonomy_gate.ACTIONS.items()
                                          if r.cls == autonomy_gate.ASK_PROD_ONLY))
def test_in_production_every_prod_only_card_asks(action, monkeypatch):
    _dev_world(monkeypatch, prod=True)
    found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 2, "recommendation": {
        "action": action, "target_id": "t", "pr_base": "main", "repo": "o/r"}})
    assert not found.acts and found.cls == autonomy_gate.ASK_PROD_RELEASE, action


@pytest.mark.parametrize("action", ["deploy_host", "deploy_stack", "review_pull_request",
                                    "approve_change_set", "infra_operation", "approve_changeset",
                                    "self_fix.install"])
def test_production_still_asks_for_deploys_merges_and_infrastructure(action, monkeypatch):
    rule = autonomy_gate.ACTIONS[action]
    assert rule.cls == autonomy_gate.ASK_PROD_ONLY
    _dev_world(monkeypatch, prod=True)
    assert not autonomy_gate.route(None, {"project_id": "p", "recommendation": {
        "action": action, "target_id": "t", "pr_base": "main", "repo": "o/r"}}).acts


def test_a_production_release_by_construction_always_asks(monkeypatch):
    _dev_world(monkeypatch, prod=False)
    for capability in ("social_publish", "promote_config"):
        found = autonomy_gate.route(None, {"project_id": "p", "recommendation": {
            "capability": capability}})
        assert not found.acts and found.cls == autonomy_gate.ASK_PROD_RELEASE, capability


def test_every_auto_decision_is_named_and_has_an_undo_or_says_why_not():
    for name, rule in [*autonomy_gate.ACTIONS.items(), *autonomy_gate.GATES.items()]:
        assert rule.agent, name
        if rule.cls not in autonomy_gate.ACTS_IN_DEV:
            continue
        if rule.undo.startswith("none:"):
            assert len(rule.undo) > len("none:") + 10, f"{name}: say why there is no undo"
        else:
            assert rule.undo in autonomy_gate.UNDO_HANDLERS, f"{name}: {rule.undo} has no handler"
    assert autonomy_gate.decided_words("deployment_manager") == \
        "Decided by Deployment manager under dev autonomy"


def test_a_tool_call_above_tier_two_or_on_untrusted_ground_still_asks(monkeypatch):
    _dev_world(monkeypatch, prod=False)
    base = {"action": "resume_agent_run"}
    assert autonomy_gate.route(None, {"risk_tier": 2, "recommendation": base}).acts
    assert not autonomy_gate.route(None, {"risk_tier": 3, "recommendation": base}).acts
    assert not autonomy_gate.route(None, {"risk_tier": 2, "recommendation": {
        **base, "tool_screen": "untrusted"}}).acts
    stop = autonomy_gate.route(None, {"risk_tier": 2, "recommendation": {
        **base, "explicit_stop": "set to ask a person each time in Skills & MCP"}})
    assert not stop.acts and stop.cls == autonomy_gate.EXPLICIT_STOP


# ---- a workflow Deploy step's gate follows its target -------------------------------------------

def _catalogue_deploy_steps():
    for entry in workflow_catalogue.CATALOGUE:
        for step in entry.get("steps") or (entry.get("spec") or {}).get("steps") or []:
            key = ((step.get("binding") or {}).get("capability") or "")
            if key in workflow_capabilities.TARGET_GATED:
                yield f"{entry['key']}:{step['id']}", key


DEPLOY_STEPS = list(_catalogue_deploy_steps())


def test_the_catalogue_has_deploy_steps_to_check():
    assert len(DEPLOY_STEPS) >= 10


@pytest.mark.parametrize("where, key", DEPLOY_STEPS)
def test_a_catalogue_deploy_gate_opens_only_for_production(where, key):
    mode = workflow_capabilities.approval_mode_for
    assert mode(key, None, target_prod=False, autonomy_acts=True) == "auto", where
    assert mode(key, None, target_prod=True, autonomy_acts=True) == "manual", where
    assert mode(key, None, target_prod=None, autonomy_acts=True) == "manual", where
    assert mode(key, None, target_prod=False, autonomy_acts=False) == "manual", where
    # The step's author asked for a person: honoured.
    assert mode(key, "manual", target_prod=False, autonomy_acts=True) == "manual", where
