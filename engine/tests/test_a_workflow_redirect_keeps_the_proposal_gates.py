"""The capability/template → start_workflow redirect must not become a hole in propose_action.

All workflow work runs through ONE generic tool: ``start_workflow(workflow_key=…, directive=…)``.
The names an agent/operator actually reaches for are a workflow's step CAPABILITY
('scan_business_rules', bound in the catalogue) or its template KEY ('business_rules_scan', the
CATALOGUE entry key). Neither is a registered tool, so today ``boss_engine.tools.builtin``'s
``propose_action`` dead-ends both with 'not an action BOSS can run'. The fix rewrites those to a
valid start_workflow offer.

The reviewers refused earlier attempts not because the redirect was wrong but because it was a
SHORTCUT: the redirect branch returned ``ok: True`` directly, jumping over the two gates every
other proposal passes — ``action_unavailable`` and ``_placeholder_argument`` — and it resolved a
capability to whatever template came first in catalogue order, so a shared capability (or a real
read tool whose name happens to equal a capability) was silently rewritten into a costly workflow
the operator never named.

This file pins the redirect AND the invariants it must not loosen:

- a UNIQUE capability / the template key becomes a valid start_workflow offer (the gap; fails on
  this checkout because propose_action dead-ends both today);
- an invented argument proposed via the capability/template name is STILL refused, exactly as the
  identical argument is refused when start_workflow is proposed directly (no asymmetry);
- a registered READ tool whose canonical name also happens to be a workflow step capability
  ('security_report', bound in the security_review template) stays a dead button — it is not
  hijacked into a workflow;
- a capability bound in MANY templates names no single workflow, so it is never silently rewritten
  to an arbitrary one (no confused-deputy widening of what a click runs).

Reached the way BOSS's own tests reach propose_action: the handler is called directly, because
ToolRegistry.invoke carries no verified identity in a unit test and would answer
identity_unavailable before the handler runs — the same approach as
test_an_offered_button_says_what_it_does.py.
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

# A registered READ tool whose canonical name is ALSO a workflow step capability (bound in the
# security_review template). The redirect must not turn it into a workflow offer.
COLLIDING_READ_TOOL = "security_report"

# A step capability bound in MANY templates: it names no single workflow, so the redirect must not
# silently rewrite it to whichever template happens to come first in catalogue order.
AMBIGUOUS_CAPABILITY = "report"

# An argument start_workflow itself refuses as fabricated (matches builtin._PLACEHOLDER_MARKERS).
INVENTED_ARG = {"target_repo": "owner/repo"}


def _propose(tool_name, *, extra=None, settings=None):
    """Call propose_action's handler directly, as BOSS's own tests do."""
    ctx = types.SimpleNamespace(settings=settings if settings is not None else get_settings())
    call_args = {"directive": "scan the example-app billing package"}
    if extra:
        call_args.update(extra)
    handler = build_default_registry().get("propose_action").handler
    return handler(ctx, project_id="p", actor="u",
                   args={"tool": tool_name, "label": "Scan for business rules",
                         "args": call_args})


# --------------------------------------------------------------------------- #
# Grounding: the names this test leans on really are what it claims
# --------------------------------------------------------------------------- #
def test_the_catalogue_and_registry_ground_this_test():
    keys = {entry.get("key") for entry in CATALOGUE}
    caps = {(step.get("binding") or {}).get("capability")
            for entry in CATALOGUE for step in (entry.get("steps") or [])}
    assert TEMPLATE_NAME in keys, f"{TEMPLATE_NAME} must be a seeded catalogue template"
    assert CAPABILITY_NAME in caps, f"{CAPABILITY_NAME} must be a catalogue step capability"

    reg = build_default_registry()
    assert COLLIDING_READ_TOOL in reg.names(), f"{COLLIDING_READ_TOOL} must be a registered tool"
    assert reg.get(COLLIDING_READ_TOOL).kind == "read", f"{COLLIDING_READ_TOOL} must be a read tool"
    assert COLLIDING_READ_TOOL in caps, (
        f"{COLLIDING_READ_TOOL} must ALSO be a workflow step capability for the collision to matter")

    ambiguous_templates = {entry.get("key") for entry in CATALOGUE
                           for step in (entry.get("steps") or [])
                           if ((step.get("binding") or {}).get("capability")) == AMBIGUOUS_CAPABILITY}
    assert len(ambiguous_templates) > 1, (
        f"{AMBIGUOUS_CAPABILITY} must be bound in several templates for the confused-deputy "
        "invariant to mean anything")


# --------------------------------------------------------------------------- #
# The gap: a UNIQUE capability / the template key becomes a valid start_workflow offer
# (fails on this checkout — propose_action dead-ends both today)
# --------------------------------------------------------------------------- #
def test_a_unique_capability_name_becomes_a_valid_start_workflow_offer():
    out = _propose(CAPABILITY_NAME)
    assert out.get("ok") is True, (
        "builtin.propose_action dead-ended the capability 'scan_business_rules' with "
        f"'not an action BOSS can run' instead of rewriting it to a start_workflow offer: {out}")
    assert out.get("proposed") is True, (
        f"builtin.propose_action must record a real offer, not refuse: {out}")
    assert out.get("tool") == STARTER_TOOL, (
        f"builtin.propose_action must redirect a workflow capability to start_workflow: {out}")
    assert out.get("args", {}).get(STARTER_FIELD) == EXPECTED_WORKFLOW, (
        f"builtin.propose_action must carry the resolved workflow in start_workflow's args: {out}")


def test_a_template_key_becomes_a_valid_start_workflow_offer():
    out = _propose(TEMPLATE_NAME)
    assert out.get("ok") is True, (
        "builtin.propose_action dead-ended the template key 'business_rules_scan' with "
        f"'not an action BOSS can run' instead of rewriting it to a start_workflow offer: {out}")
    assert out.get("proposed") is True, (
        f"builtin.propose_action must record a real offer, not refuse: {out}")
    assert out.get("tool") == STARTER_TOOL, (
        f"builtin.propose_action must redirect a template key to start_workflow: {out}")
    assert out.get("args", {}).get(STARTER_FIELD) == EXPECTED_WORKFLOW, (
        f"builtin.propose_action must carry the resolved workflow in start_workflow's args: {out}")


# --------------------------------------------------------------------------- #
# Invariant: the redirect still runs the placeholder gate — no invented-argument hole
# --------------------------------------------------------------------------- #
def test_an_invented_argument_via_the_capability_is_refused_like_a_direct_start_workflow():
    """The exact asymmetry the reviewers flagged: an invented argument that start_workflow itself
    refuses must not sail through when the identical workflow is reached by its capability name."""
    direct = _propose(STARTER_TOOL, extra={STARTER_FIELD: EXPECTED_WORKFLOW, **INVENTED_ARG})
    assert direct.get("ok") is False, (
        "grounding: builtin.propose_action's _placeholder_argument gate must refuse start_workflow "
        f"proposed directly with an invented argument: {direct}")

    via_capability = _propose(CAPABILITY_NAME, extra=dict(INVENTED_ARG))
    assert via_capability.get("ok") is not True, (
        "builtin.propose_action let an invented argument through the capability redirect that "
        f"start_workflow itself refuses — the redirect must not skip the placeholder gate: {via_capability}")
    assert via_capability.get("proposed") is not True, (
        f"builtin.propose_action must not record an offer carrying an invented argument: {via_capability}")


def test_an_invented_argument_via_the_template_key_is_refused_too():
    via_template = _propose(TEMPLATE_NAME, extra=dict(INVENTED_ARG))
    assert via_template.get("ok") is not True, (
        "builtin.propose_action let an invented argument through the template-key redirect — the "
        f"redirect must not skip the placeholder gate: {via_template}")
    assert via_template.get("proposed") is not True, (
        f"builtin.propose_action must not record an offer carrying an invented argument: {via_template}")


# --------------------------------------------------------------------------- #
# Invariant: a registered read tool is never hijacked, even if its name collides with a capability
# --------------------------------------------------------------------------- #
def test_a_read_tool_whose_name_collides_with_a_capability_is_still_a_no_op():
    """'security_report' is a registered read tool AND a step capability of security_review.
    builtin.propose_action must keep dead-ending it, not substitute a costly workflow run."""
    out = _propose(COLLIDING_READ_TOOL)
    assert out.get("ok") is False, (
        "builtin.propose_action hijacked the registered read tool 'security_report' into a "
        f"workflow offer; a read tool behind a button must stay a dead button: {out}")
    assert out.get("tool") != STARTER_TOOL, (
        f"builtin.propose_action must not rewrite the read tool 'security_report' to start_workflow: {out}")
    assert (out.get("args") or {}).get(STARTER_FIELD) is None, (
        f"builtin.propose_action must not hand a read tool a workflow key: {out}")
    assert "not an action BOSS can run" in str(out.get("error", "")), (
        f"the dead-end refusal must still name the behaviour: {out}")


# --------------------------------------------------------------------------- #
# Invariant: an ambiguous capability names no single workflow, so it is never silently redirected
# --------------------------------------------------------------------------- #
def test_a_capability_bound_in_many_templates_is_not_silently_redirected():
    """A capability shared by many templates ('report' is a step in 13) does not identify one
    workflow. builtin.propose_action must not silently rewrite it to whichever template comes first
    — an operator's single click must not kick off a bigger workflow than the offer described."""
    out = _propose(AMBIGUOUS_CAPABILITY)
    assert not (out.get("ok") is True and out.get("tool") == STARTER_TOOL), (
        "builtin.propose_action silently rewrote an ambiguous capability into an arbitrary "
        f"start_workflow offer (confused-deputy widening): {out}")
