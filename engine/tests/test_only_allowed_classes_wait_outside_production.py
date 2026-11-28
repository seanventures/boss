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
  * ask_product_choice — a blocking product choice the spec panel could not settle from the
    brief (public or administrators only?), asked BEFORE the build starts. Enterprise
    validation, ENT-REC-49: the dev product manager approved a spec whose panel had asked
    exactly that, and the build guessed. Only a parsed `BUILD READINESS: ASK` sets it.
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
                                      "ask_prod_release", "ask_product_choice"})


def test_the_allowed_waits_are_exactly_the_operators_four_plus_explicit_stops():
    assert autonomy_gate.ALLOWED_DEV_WAITS == PINNED_ALLOWED_DEV_WAITS


#: EVERY CARD THAT STILL WAITS OUTSIDE PRODUCTION, BY NAME. Moving an action into a wait class
#: (or adding one) fails here until this list is edited, next to the operator's words: that edit
#: is the review of a new ask.
PINNED_WAITS = {
    "ask_credential": {
        "configure_supported_channel", "configure_target_github_and_retry_phase",
        "provide_github_token", "provide_openai_api_key", "retry_when_secret_set",
        "review_entitlement_sources", "sign_in_to_subscription_again", "store_proposed_secret",
        # Raised only when no safe move is left: a sign-in, a host or a key in a person's hand.
        "stuck_work_needs_person"},
    "ask_prod_release": {
        "github.community.merge_authorization", "promote_agent_deployment",
        # Widening egress or a data boundary reaches outside; a changed tool definition is
        # untrusted; a standing grant or an autonomy raise is authority that reaches production.
        "allow_repository_or_change_clone_target", "rules_forbidden",
        "reclassify_or_configure_approved_boundary_adapter", "review_tool_change",
        "grant_builder_authority", "role.autonomy_change",
        # They change the agents production work runs with (autonomy_gate.PERSON_ONLY_ACTIONS,
        # #307 review P1-3): an agent-drafted skill, an agent's prompt, a new hire.
        "activate_authored_skill", "apply_agent_prompt_diff", "role.hire"},
    "ask_spend": {
        "approve_budget_increase_or_accept_degraded_mode", "approve_initiative_budget",
        "budget_denied", "budget_tier2_frozen", "configure_or_enable_infra_provisioning",
        "increase_allocation_or_defer", "increase_budget_or_defer",
        "increase_budget_or_defer_work", "model_capacity_denied", "model_capacity_exhausted",
        "raise_cap_or_wait_for_headroom", "restore_model_capacity_or_raise_budget",
        "review_cost_anomaly", "review_provider_spend_kill_switch",
        "set_BOSS_INFRA_REAL_PROVISION_ALLOWED",
        # A paid or host-effecting call whose outcome is unknown: approving re-runs it.
        "architecture_reconciliation", "conversation_reconciliation",
        "discovery_reconciliation", "reconcile_legacy_model_exposure",
        "reconcile_legacy_provider_exposure", "review_model_spend_and_host_effects_before_retry",
        "review_model_spend_and_receipts_before_retry", "typed_decision_lease_expired",
        "typed_decision_reconciliation", "verify_provider_outcome_before_retry"},
    "explicit_stop": {"review_always_on_build"},
    # Decided per card, not per action (autonomy_gate._product_choice): no action waits for it.
    "ask_product_choice": set(),
}
PINNED_GATE_WAITS = {"agent_work": "explicit_stop", "promote_config": "ask_prod_release",
                     "social_publish": "ask_prod_release",
                     # A release gate a workflow's author put there (#307 review P1-3).
                     "release_approval": "explicit_stop"}
PINNED_KIND_WAITS = {"app_secret_live": "ask_credential"}


def test_the_cards_that_wait_outside_production_are_exactly_the_pinned_ones():
    found: dict[str, set[str]] = {c: set() for c in PINNED_ALLOWED_DEV_WAITS}
    for action, rule in autonomy_gate.ACTIONS.items():
        if rule.cls in autonomy_gate.ALLOWED_DEV_WAITS:
            found[rule.cls].add(action)
    assert found == PINNED_WAITS
    assert {k: r.cls for k, r in autonomy_gate.GATES.items()
            if r.cls in autonomy_gate.ALLOWED_DEV_WAITS} == PINNED_GATE_WAITS
    assert {k: r.cls for k, r in autonomy_gate.KINDS.items()
            if r.cls in autonomy_gate.ALLOWED_DEV_WAITS} == PINNED_KIND_WAITS
    for prefix, rule in autonomy_gate.PREFIXES.items():
        # configure_or_enable_<provider>: turning a channel provider on is a credential.
        assert (prefix, rule.cls) == ("configure_or_enable_", "ask_credential")


#: Producers the scan cannot read an action from, each with what its card carries. A new one
#: fails until it is read here.
UNRESOLVED_OK = {
    # agent_loop's held calls: action "resume_agent_run" written before **bind_held_call,
    # which adds the call's binding and no `action` key.
    "engine/boss_engine/services/agent_loop.py",
    # config_deploy: action is governed.DECISION_ACTION ("deploy_host"), classified.
    "engine/boss_engine/services/config_deploy.py",
}


def test_no_producer_writes_a_card_the_autonomy_check_cannot_read():
    unread = sorted({f.where.rsplit(":", 1)[0] for f in scan.ALL_FINDINGS if f.unresolved}
                    - UNRESOLVED_OK)
    assert not unread, f"read these producers' actions in the scan: {unread}"
    from boss_engine.services import governed_host_deploy

    assert governed_host_deploy.DECISION_ACTION in autonomy_gate.ACTIONS
    assert "resume_agent_run" in autonomy_gate.ACTIONS


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


#: The cards that write no `action` (scan.NO_ACTION_BY_DESIGN), each as its producer writes it.
NO_ACTION_SHAPES = {
    ("engine/boss_engine/services/sre_runbooks.py", "_hold_decision"):
        {"kind": "sre_runbook"},
    ("engine/boss_engine/services/canonical.py", "_create_meeting_decision"):
        {"meeting_id": "mtg_1", "summary": "x"},
    ("engine/boss_engine/templates/secrets.py", "_live_decision"):
        {"kind": "app_secret_live"},
    ("engine/boss_engine/services/ambient/board.py", "needs_you"):
        {"action": "incident_squad.mitigate", "work_item_id": "w", "asked_by": "a"},
    ("engine/boss_engine/services/ambient/board.py", "needs_you_batch"):
        {"action": "anything_an_agent_asks", "work_item_id": "w", "asked_by": "a"},
    ("engine/boss_engine/services/ambient/asks.py", "raise_card"):
        {"action": "ambient_card", "work_item_id": "w", "asked_by": "a"},
}


def test_every_card_without_an_action_is_classified_by_its_shape(monkeypatch):
    assert set(NO_ACTION_SHAPES) == set(scan.NO_ACTION_BY_DESIGN)
    _dev_world(monkeypatch, prod=False)
    for where, rec in NO_ACTION_SHAPES.items():
        assert autonomy_gate.rule_for({"recommendation": rec}) is not None, where
        found = autonomy_gate.route(None, {"project_id": "p", "recommendation": rec})
        assert found.cls != "unclassified", where
        assert found.acts or found.cls in autonomy_gate.ALLOWED_DEV_WAITS, (where, found)


def test_every_catalogue_gate_capability_is_listed_by_name():
    """No catalogue gate falls to the custom-step fallback (GATE_DEFAULT)."""
    gates = {c.key for c in workflow_capabilities.CAPABILITIES.values()
             if c.is_gate or c.risk_tier >= workflow_capabilities.MANUAL_APPROVAL_RISK_FLOOR}
    assert gates <= set(autonomy_gate.GATES), sorted(gates - set(autonomy_gate.GATES))


#: Card fields that make a card wait whatever its class, and the modules that write them.
PERSON_FORCING_KEYS = ("explicit_stop", "approval_mode_requested", "requires_person",
                       "requires_person_approval", "ci_changed")
PINNED_PERSON_FORCING_WRITERS = {
    "apps/api/app/main.py", "apps/api/app/routers/ci.py",
    "engine/boss_engine/identity/approval_delegations.py",
    "engine/boss_engine/services/agent_loop.py", "engine/boss_engine/services/autonomy_gate.py",
    "engine/boss_engine/services/ci_gate.py", "engine/boss_engine/services/meeting_service.py",
    "engine/boss_engine/services/ruleset_changes.py",
    "engine/boss_engine/services/skill_catalogue.py",
    "engine/boss_engine/services/software_factory.py",
    "engine/boss_engine/services/work_agreement.py",
    "engine/boss_engine/services/workflow_capabilities.py",
    "engine/boss_engine/services/workflow_capability_runner.py",
    "engine/boss_engine/services/workflow_custom_steps.py",
    "engine/boss_engine/services/workflows.py", "engine/boss_engine/tools/builtin.py",
}


def test_the_modules_that_can_force_a_wait_are_pinned():
    from pathlib import Path

    root = Path(scan.ROOT)
    found = set()
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in (root / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(f'"{k}"' in text for k in PERSON_FORCING_KEYS):
                found.add(str(path.relative_to(root)))
    assert found == PINNED_PERSON_FORCING_WRITERS, (
        "a module now writes (or stops writing) a key that makes a card wait: review it and "
        f"edit this list. new: {sorted(found - PINNED_PERSON_FORCING_WRITERS)}, gone: "
        f"{sorted(PINNED_PERSON_FORCING_WRITERS - found)}")


def _dev_world(monkeypatch, *, prod: bool) -> list:
    """The default policy, and the resolver answering `prod` — recording the scope each card
    hands it, so a test can check the card asked about the right place."""
    monkeypatch.setattr(autonomy_policy, "load", lambda *a, **k: autonomy_policy.DEFAULT)
    verdict = production.Verdict(prod, True, "prod" if prod else "staging", "test", "test")
    scopes: list = []

    def resolve(connection, scope, **kwargs):
        scopes.append(dict(scope))
        return verdict
    monkeypatch.setattr(production, "resolve", resolve)
    return scopes


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
    scopes = _dev_world(monkeypatch, prod=True)
    assert not autonomy_gate.route(None, {"project_id": "p", "recommendation": {
        "action": action, "target_id": "t", "pr_base": "main", "repo": "o/r"}}).acts
    # ... and it asked about the right place.
    expected = {"merge": {"pr_base": "main", "repo": "o/r"}, "install": {"install": True},
                "target": {"target_id": "t"}}[rule.scope]
    assert scopes == [expected], (action, scopes)


def test_a_merge_card_naming_no_base_asks_about_the_repositorys_default_branch(monkeypatch):
    scopes = _dev_world(monkeypatch, prod=True)
    autonomy_gate.route(None, {"project_id": "p", "recommendation": {
        "action": "review_pull_request",
        "pull_request_url": "https://github.com/acme/app/pull/7"}})
    assert scopes == [{"pr_base": production.DEFAULT_BRANCH, "repo": "acme/app"}]


def test_a_local_change_set_is_development_by_where_it_lives():
    assert autonomy_gate._scope_of(  # noqa: SLF001
        {"pull_request_url": "http://boss-local/local_p/app/changes/3"},
        autonomy_gate.ACTIONS["review_pull_request"])["host"] == "localhost"


def test_an_unknown_gate_capability_never_acts_in_production(monkeypatch):
    """A custom step's gate (GATE_DEFAULT) at tier 3 asks; listed gates follow their rule."""
    _dev_world(monkeypatch, prod=True)
    found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 3, "recommendation": {
        "capability": "a_custom_step"}})
    assert not found.acts


@pytest.mark.parametrize("prod", [False, True])
def test_a_production_release_by_construction_always_asks(monkeypatch, prod):
    _dev_world(monkeypatch, prod=prod)
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


# ---- #307 review: the two-person rule, person-only actions, stops a person set ---------------

@pytest.mark.parametrize("card", [
    # The catalogue's security review gate (risk tier 3).
    {"capability": "security_review"},
    # The findings park ("Continue this workflow?", workflow_capability_runner, tier 3), which
    # GATE_DEFAULT's `not findings` exception used to let through.
    {"capability": "a_custom_step", "findings": [{"severity": "high"}]},
    # An action that acts at tier 2.
    {"action": "resume_code_task"},
    {"action": "review_pull_request", "pr_base": "rel/x", "repo": "o/r"},
])
def test_a_two_person_tier_card_is_never_answered_by_autonomy(card, monkeypatch):
    """transition(system_policy=True) skips approvals.require_approval, so route() itself asks
    for every tier the two-person rule covers, whatever the card's class or environment."""
    from boss_engine.identity.approvals import TWO_PERSON_TIERS

    _dev_world(monkeypatch, prod=False)
    for tier in sorted(TWO_PERSON_TIERS):
        found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": tier,
                                           "recommendation": card})
        assert not found.acts and found.cls in autonomy_gate.ALLOWED_DEV_WAITS, (card, found)
        assert "two-person" in found.why
    # The same card one tier lower is not held by this rule.
    found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 2, "recommendation": card})
    assert "two-person" not in found.why


def test_person_only_actions_ask_and_are_one_list_with_the_settings_list(monkeypatch):
    _dev_world(monkeypatch, prod=False)
    assert autonomy_gate.PERSON_ONLY_ACTIONS == {"activate_authored_skill",
                                                 "apply_agent_prompt_diff", "role.hire"}
    assert autonomy_gate.PERSON_ONLY_ACTIONS <= autonomy_gate.SETTING_NEVER_APPROVES
    for action in autonomy_gate.PERSON_ONLY_ACTIONS:
        found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 1,
                                           "recommendation": {"action": action}})
        assert not found.acts, action
    # decisions.approve_by_policy reads the one list, not a copy of its own.
    import inspect

    from boss_engine.services import decisions

    source = inspect.getsource(decisions.approve_by_policy)
    assert "autonomy_gate.SETTING_NEVER_APPROVES" in source
    assert "activate_authored_skill" not in source


@pytest.mark.parametrize("prod", [False, True])
def test_a_stop_class_rule_never_acts(monkeypatch, prod):
    """A rule whose class is explicit_stop asks — it used to fall through to act."""
    _dev_world(monkeypatch, prod=prod)
    for capability in ("release_approval", "agent_work"):
        found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 2,
                                           "recommendation": {"capability": capability}})
        assert not found.acts and found.cls == autonomy_gate.EXPLICIT_STOP, capability
    found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 1,
                                       "recommendation": {"action": "review_always_on_build"}})
    assert not found.acts and found.cls == autonomy_gate.EXPLICIT_STOP


def test_a_message_gate_asks_unless_it_goes_to_the_teams_own_channel(monkeypatch):
    monkeypatch.setattr(autonomy_policy, "load", lambda *a, **k: autonomy_policy.DEFAULT)
    for capability in ("notify", "github_comment"):
        for channel, acts in (("slack:C123", True), ("mattermost:abc", True),
                              ("email:someone@example.com", False),
                              ("github:acme/app#12", False), ("", False)):
            found = autonomy_gate.route(None, {"project_id": "p", "risk_tier": 1,
                                               "recommendation": {"capability": capability,
                                                                  "channel": channel}})
            assert found.acts is acts, (capability, channel, found)


def test_only_a_parsed_panel_question_waits_as_a_product_choice():
    """ask_product_choice is decided per card: a PRD card carrying the panel's parsed question
    waits under Agents act; the same card without one (ready, or assumed ready) acts."""
    policy = autonomy_policy.coerce({"dev": "act"})
    card = {"project_id": "", "risk_tier": 2, "recommendation": {
        "action": "execute_agent_tool", "tool": "publish_document",
        "args": {"title": "PRD: x", "markdown": "# PRD"}}}
    assert autonomy_gate.route(None, card, policy=policy).acts
    asked = {**card, "recommendation": {**card["recommendation"],
                                         "build_readiness_question": "Public or admins only?"}}
    found = autonomy_gate.route(None, asked, policy=policy)
    assert (found.acts, found.cls) == (False, "ask_product_choice")
    assert "Public or admins only?" in found.why and found.what_to_do
    other = {**asked, "recommendation": {**asked["recommendation"], "tool": "send_email"}}
    assert autonomy_gate.route(None, other, policy=policy).cls != "ask_product_choice"
