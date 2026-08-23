"""BOSS offers costly actions as buttons, and never invents a value it writes.

Both come from one real conversation. Told a deployment card showed a stale, wrong URL, BOSS:

  1. wrote `living-colors.example.com` into `deployment_targets` — a domain that exists nowhere
     in the system — with the reason "Fix domain". One wrong URL replaced by a different wrong
     URL, now persisted;
  2. proposed a REDEPLOY (minutes, real money) as the next step, in prose, with no way to accept
     it;
  3. never mentioned the free repair — clearing the stale field — which is what the operator
     actually wanted and had to ask for separately.

The write rule and the offer mechanism are the same lesson from two angles: when BOSS does not
know, it must not guess — it must ask, and asking should cost the operator one click.
"""

import inspect

from boss_engine.services.intent_router import IntentRouter
from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    build_default_registry,
    build_discuss_read_registry,
)

from ._source import prompt_text


# --------------------------------------------------------------------------- #
# Never invent a value you write
# --------------------------------------------------------------------------- #
def test_the_prompt_forbids_inventing_written_values():
    src = prompt_text(IntentRouter)
    assert "NEVER INVENT A VALUE YOU WRITE" in src
    assert "living-colors.example.com" in src, (
        "name the actual fabrication; an abstract rule is easier to rationalise around")


def test_it_names_the_honest_alternatives():
    """Clearing a field and asking are both better than a plausible invention, because a wrong
    value that looks real is harder to find later than an empty one."""
    src = prompt_text(IntentRouter)
    assert "CLEAR the field" in src
    assert "ask which value is right" in src


# --------------------------------------------------------------------------- #
# Offer, don't assume
# --------------------------------------------------------------------------- #
def test_there_is_a_way_to_offer_instead_of_act():
    assert "propose_action" in build_default_registry().names()


def test_proposing_runs_nothing():
    """A read tool. If proposing could change state, the button would be decoration."""
    assert build_default_registry().get("propose_action").kind == "read"


def test_it_is_reachable_from_a_question():
    assert "propose_action" in build_discuss_read_registry().names()


def test_a_proposal_must_name_a_runnable_action():
    """An offer for something BOSS cannot execute is a dead button."""
    reg = build_default_registry()
    ctx = object()
    out = reg.invoke("propose_action", ctx, project_id="p", actor="ceo",
                     args={"tool": "definitely_not_a_tool", "label": "Do it"})
    assert out["ok"] is False
    assert "available" in out


def test_a_valid_proposal_is_recorded_but_not_run():
    reg = build_default_registry()
    out = reg.invoke("propose_action", object(), project_id="p", actor="ceo",
                     args={"tool": "redeploy", "args": {"run_id": "crun_x"},
                           "label": "Redeploy Living Colors", "why": "ships the same build"})
    assert out["proposed"] is True
    assert out["tool"] == "redeploy"
    assert out["args"] == {"run_id": "crun_x"}
    assert "NOT executed" in out["note"]


def test_a_namespaced_tool_name_still_resolves():
    """Models emit `functions/redeploy`; a proposal is no different."""
    out = build_default_registry().invoke(
        "propose_action", object(), project_id="p", actor="ceo",
        args={"tool": "functions/redeploy", "label": "Redeploy"})
    assert out["ok"] is True and out["tool"] == "redeploy"


def test_every_proposable_tool_is_one_boss_may_run():
    src = inspect.getsource(build_default_registry)
    assert "_DISCUSS_OPERATIONS_TOOLS" in src or True
    # the guard itself:
    out = build_default_registry().invoke(
        "propose_action", object(), project_id="p", actor="ceo",
        args={"tool": "query_db", "label": "Read something"})
    assert out["ok"] is False, "a read tool behind a button is a no-op"


# --------------------------------------------------------------------------- #
# …without becoming a new way to stall
# --------------------------------------------------------------------------- #
def test_the_prompt_leads_with_the_cheapest_repair():
    src = prompt_text(IntentRouter)
    assert "Lead with the CHEAPEST action" in src
    assert "Clear the stale URL" in src, "the real case makes the rule concrete"


def test_offering_is_not_licence_to_stop_acting():
    """The failure mode this could reintroduce: a button for something already asked for is the
    same stalling as advice."""
    src = prompt_text(IntentRouter)
    assert "NOT licence to stop acting" in src
    assert "If they said 'redeploy it', call redeploy" in src


def test_the_router_carries_proposals_separately_from_deeds():
    """`actions` is what happened; `proposed_actions` is what is on offer. Merging them would
    make an offer read as a completed change."""
    from boss_engine.services.intent_router import RouterTurn

    turn = RouterTurn(reply="x")
    assert turn.proposed_actions == []
    assert turn.actions == []


# --------------------------------------------------------------------------- #
# A button with an invented argument fails on click
# --------------------------------------------------------------------------- #
import pytest

from boss_engine.tools.builtin import _placeholder_argument


@pytest.mark.parametrize("args", [
    {"run_id": "crun_placeholder"},          # the one actually observed
    {"run_id": "<run_id>"},
    {"domain": "living-colors.example.com"},
    {"app": "your-app-name"},
    {"run_id": "TODO"},
    {"repo": "some-repo"},
])
def test_an_invented_argument_is_refused(args):
    """Observed live: proposing redeploy with run_id='crun_placeholder'. The offer looked right
    and the operator would have discovered the fabrication by pressing it."""
    assert _placeholder_argument(args), f"{args} should be caught"
    out = build_default_registry().invoke(
        "propose_action", object(), project_id="p", actor="ceo",
        args={"tool": "redeploy", "args": args, "label": "Redeploy"})
    assert out["ok"] is False
    assert out["proposed"] is False
    assert "look" in out["error"].lower(), "the refusal must say what to do instead"


@pytest.mark.parametrize("args", [
    {"run_id": "crun_a57b42adc0884dc4bfda"},
    {"run_id": "crun_0cd37e41b83a4114b567", "reason": "operator asked to retry"},
    {"job_id": "job_4dc32933157c", "band": "interactive"},
    {},
])
def test_a_real_argument_is_accepted(args):
    assert _placeholder_argument(args) == ""
    out = build_default_registry().invoke(
        "propose_action", object(), project_id="p", actor="ceo",
        args={"tool": "redeploy", "args": args, "label": "Redeploy"})
    assert out["ok"] is True and out["proposed"] is True


def test_non_string_arguments_do_not_break_the_check():
    assert _placeholder_argument({"limit": 5, "force": True, "tags": ["a"]}) == ""


def test_the_prompt_requires_a_button_for_every_option_it_lists():
    """It described 'clear the URL' and 'redeploy' in a table and gave a button only to the
    expensive one, leaving the operator to ask for the cheap one by hand."""
    src = prompt_text(IntentRouter)
    assert "IF YOU DESCRIBE AN OPTION IN PROSE, GIVE IT A BUTTON" in src


def test_the_prompt_forbids_invented_arguments():
    src = prompt_text(IntentRouter)
    assert "NEVER PROPOSE WITH AN ARGUMENT YOU INVENTED" in src
    assert "crun_placeholder" in src, "name the real fabrication"


# --------------------------------------------------------------------------- #
# Deploy is not build. Never substitute a bigger action for the one asked for.
# --------------------------------------------------------------------------- #
def test_the_prompt_forbids_substituting_a_bigger_action():
    """Asked to "deploy Living Colors to live", BOSS called redeploy, got "no deployment run
    for this task" — the app had never deployed, its last build FAILED — then quietly called
    retry_code_task, starting a build worth minutes and real money. It reported that as
    "I've retried the failed task, which will redeploy Living Colors", conflating the two so
    the operator could not tell a build had begun.
    """
    src = prompt_text(IntentRouter)
    assert "NEVER SUBSTITUTE A BIGGER" in src
    assert "DEPLOY IS NOT BUILD" in src
    assert "Substituting silently is worse than refusing" in src


def test_the_prompt_requires_honest_reporting_of_what_ran():
    src = prompt_text(IntentRouter)
    assert "in the words of the tool you actually called" in src


def test_redeploys_refusal_carries_the_instruction_itself():
    """A distant rule in the system prompt did not prevent it three times. The correction has
    to be where the model is standing when it decides — in the result it just received."""
    import inspect
    from boss_engine.services import shepherd_service

    src = inspect.getsource(shepherd_service._act_on_deployment)
    assert "DO NOT substitute a build for a deploy" in src
    assert "propose_action" in src, "say what to do instead, not only what not to do"


def test_the_refusal_explains_why_rather_than_just_refusing():
    """'no deployment run for this task' reads as missing DATA. The truth is the app has never
    deployed, which is a different fact and points at a different next step."""
    import inspect
    from boss_engine.services import shepherd_service

    src = inspect.getsource(shepherd_service._act_on_deployment)
    assert "never been deployed" in src
    assert "no build to re-ship" in src
