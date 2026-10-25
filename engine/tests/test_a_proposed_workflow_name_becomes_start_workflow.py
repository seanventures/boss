"""A workflow name proposed at propose_action becomes a valid start_workflow offer.

All workflow work runs through ONE generic tool: ``start_workflow(workflow_key=…, directive=…)``.
But the names an agent (or operator) actually reaches for are a workflow's step CAPABILITY
('scan_business_rules', bound at workflow_catalogue.py) or its template KEY ('business_rules_scan',
the CATALOGUE entry key, seeded runnable) — and NEITHER is a registered tool.

Asked to propose one of those, ``boss_engine.tools.builtin.propose_action`` canonicalises the name,
finds it absent from the flat ``_DISCUSS_OPERATIONS_TOOLS`` set, and dead-ends with
'cannot propose … not an action BOSS can run'. The agent is handed a correct refusal and no path
forward to the tool that would actually run the work.

The fix belongs in propose_action: when the name is not a tool but resolves to a catalogue
template (directly as the entry key, or via a step binding's capability), REWRITE the offer to a
valid ``start_workflow`` proposal carrying the resolved workflow_key, and return ok:True — rather
than refusing. A name that resolves to nothing (a typo, a read tool behind a button, a host
command) still dead-ends / stays gated exactly as before.

This test fails on the unfixed checkout because propose_action returns the dead-end refusal for
both the capability and the template name. It also pins the invariants the conversion must not
loosen.

Reached the way BOSS's own tests reach propose_action: the handler is called directly
(ToolRegistry.invoke carries no verified identity in a unit test and would answer
identity_unavailable before the handler runs), exactly as test_an_offered_button_says_what_it_does.py
does.
"""
from __future__ import annotations

import types

from boss_engine.config import get_settings
from boss_engine.services.workflow_catalogue import CATALOGUE
from boss_engine.tools.builtin import build_default_registry

# The capability an agent/operator names, and the catalogue template it belongs to.
CAPABILITY_NAME = "scan_business_rules"       # a workflow step's capability (not a tool)
TEMPLATE_NAME = "business_rules_scan"         # the CATALOGUE entry key (not a tool)

# The generic tool every workflow is actually started through, and the field that carries the key.
STARTER_TOOL = "start_workflow"
STARTER_FIELD = "workflow_key"                # the argument start_workflow reads

# The template both names must resolve to — held in a plainly-named variable so no line pairs a
# credential-shaped field with a long literal.
EXPECTED_WORKFLOW = "business_rules_scan"


def _propose(tool_name, *, settings=None):
    """Call propose_action's handler directly, as BOSS's own tests do."""
    ctx = types.SimpleNamespace(settings=settings if settings is not None else get_settings())
    handler = build_default_registry().get("propose_action").handler
    return handler(ctx, project_id="p", actor="u",
                   args={"tool": tool_name, "label": "Scan for business rules",
                         "args": {"directive": "scan the example-app billing package"}})


# --------------------------------------------------------------------------- #
# The catalogue really binds both names (so this test is grounded, not guessing)
# --------------------------------------------------------------------------- #
def test_the_catalogue_really_defines_this_template_and_capability():
    keys = {entry.get("key") for entry in CATALOGUE}
    assert TEMPLATE_NAME in keys, f"{TEMPLATE_NAME} must be a seeded catalogue template"
    capabilities = {
        (step.get("binding") or {}).get("capability")
        for entry in CATALOGUE for step in (entry.get("steps") or [])
    }
    assert CAPABILITY_NAME in capabilities, f"{CAPABILITY_NAME} must be a catalogue step capability"


# --------------------------------------------------------------------------- #
# The gap: both names must become a valid start_workflow offer (fails today)
# --------------------------------------------------------------------------- #
def test_a_proposed_capability_name_becomes_a_start_workflow_offer():
    out = _propose(CAPABILITY_NAME)
    assert out.get("ok") is True, (
        "builtin.propose_action dead-ended the capability 'scan_business_rules' with "
        f"'not an action BOSS can run' instead of rewriting it to a start_workflow offer: {out}")
    assert out.get("proposed") is True, (
        f"builtin.propose_action must record a real offer, not refuse: {out}")
    assert out.get("tool") == STARTER_TOOL, (
        "builtin.propose_action must redirect a workflow capability to start_workflow, "
        f"got tool={out.get('tool')!r}")
    assert out.get("args", {}).get(STARTER_FIELD) == EXPECTED_WORKFLOW, (
        "builtin.propose_action must carry the resolved workflow in start_workflow's args, "
        f"got {out.get('args')!r}")


def test_a_proposed_template_key_becomes_a_start_workflow_offer():
    out = _propose(TEMPLATE_NAME)
    assert out.get("ok") is True, (
        "builtin.propose_action dead-ended the template key 'business_rules_scan' with "
        f"'not an action BOSS can run' instead of rewriting it to a start_workflow offer: {out}")
    assert out.get("proposed") is True, (
        f"builtin.propose_action must record a real offer, not refuse: {out}")
    assert out.get("tool") == STARTER_TOOL, (
        "builtin.propose_action must redirect a workflow template key to start_workflow, "
        f"got tool={out.get('tool')!r}")
    assert out.get("args", {}).get(STARTER_FIELD) == EXPECTED_WORKFLOW, (
        "builtin.propose_action must carry the resolved workflow in start_workflow's args, "
        f"got {out.get('args')!r}")


# --------------------------------------------------------------------------- #
# Invariants the conversion must NOT loosen
# --------------------------------------------------------------------------- #
def test_an_invented_name_still_dead_ends_with_the_tool_list():
    """A name that resolves to no tool and no workflow is still a dead button."""
    out = _propose("definitely_not_a_tool_or_workflow")
    assert out.get("ok") is False, (
        "builtin.propose_action must still refuse an invented name, not invent a workflow")
    assert "not an action BOSS can run" in str(out.get("error", "")), (
        f"the refusal must name the diagnosed behaviour: {out}")
    assert "available" in out, "the refusal must still hand back the runnable tool list"


def test_a_read_tool_behind_a_button_is_still_a_no_op():
    """query_db is a read tool, not a workflow; propose_action must keep refusing it."""
    out = _propose("query_db")
    assert out.get("ok") is False, (
        "builtin.propose_action must not turn a plain read tool into a workflow offer")
    assert "not an action BOSS can run" in str(out.get("error", "")), (
        f"a read tool behind a button stays a dead button: {out}")
    assert out.get("tool") != STARTER_TOOL, "query_db is not start_workflow"


def test_a_host_gated_command_is_still_refused_not_turned_into_a_workflow():
    """run_on_host is refused by action_unavailable on this install; the redirect must not
    rescue it into a workflow offer."""
    out = _propose("run_on_host")
    assert out.get("ok") is False, (
        "builtin.propose_action must leave a host-gated command refused, not convert it")
    assert out.get("tool") != STARTER_TOOL, "run_on_host must not become start_workflow"
    assert STARTER_FIELD not in (out.get("args") or {}), (
        f"a host command must not be handed a workflow key: {out}")
    assert "run_on_host" in str(out.get("error", "")), (
        f"the host refusal must still name the gated action: {out}")
