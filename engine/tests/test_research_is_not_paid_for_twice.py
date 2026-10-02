"""Finished research carries forward instead of being commissioned again.

"I want to take that PRD and go to implementation instead of repeating the research again."

The runtime could already do it — `adopt_discovery_run_id` makes the research step wait on a
dossier that exists rather than starting another. What it could not do is be ASKED: the field is
an `adopt_*` key, and every client's `adopt_*` keys are stripped at the API, deliberately, so a
caller cannot assert that work has already happened. (That rule earned itself: "Run this
workflow again" once copied the previous run's input whole and adopted the same finished build —
research recorded as not-run and `build` green in seconds against yesterday's code.)

So the caller NAMES A SOURCE and the server draws the conclusion.
"""
import pytest

from boss_engine.tools.builtin import build_discuss_read_registry


def test_the_agent_can_be_told_which_research_to_build_on():
    spec = build_discuss_read_registry().get("start_workflow")
    props = (spec.parameters or {}).get("properties") or {}
    assert "from_discovery" in props
    assert "adopt_discovery_run_id" not in props, (
        "the adopt key is the server's conclusion, never the caller's claim")


def test_and_is_told_to_look_the_id_up_rather_than_guess():
    spec = build_discuss_read_registry().get("start_workflow")
    described = ((spec.parameters or {}).get("properties") or {})["from_discovery"]["description"]
    assert "list_discovery" in described
    assert "never guess" in described


def test_the_research_step_still_knows_how_to_adopt():
    # The half that already worked. If this table loses its research row, everything above
    # becomes a run that pays for research twice while reporting that it did not.
    from boss_engine.services.workflow_capability_runner import _ADOPTABLE

    assert _ADOPTABLE["research"] == ("adopt_discovery_run_id", "discovery")


def test_an_adopted_step_still_yields_the_dossier_for_the_panel():
    """Adoption that produced only "done" would leave the panel debating the one-line directive
    as though no research had happened — which is what research was for."""
    import inspect

    from boss_engine.services import workflow_capability_runner as runner

    source = inspect.getsource(runner)
    assert '"dossier": _dossier_brief(' in source
    assert '"discovery_id": awaited_id' in source


@pytest.mark.parametrize("field", ["from_run", "from_discovery"])
def test_the_api_resolves_a_named_source_rather_than_trusting_a_claim(field):
    """REWRITTEN (review of 0fd6c45). This pinned the endpoint's own copy of the rule —
    `_run_actions.require(` for from_run and its own "not completed" check for from_discovery,
    which accepted a dossier with NO findings and was skipped entirely when both were named.
    The rule now lives once, in `workflow_run_actions.research_to_adopt`, which the agent's
    start_workflow asks too; the behaviour is enforced in
    test_a_caller_cannot_claim_work_happened.py."""
    import inspect
    from pathlib import Path

    from boss_engine.services import workflow_run_actions as actions

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    source = api.read_text(encoding="utf-8")
    assert f'payload.get("{field}")' in source
    assert "_run_actions.research_to_adopt(" in source
    rule = inspect.getsource(actions.research_to_adopt)
    assert 'require(connection, project_id, run_id, "build_on_research")' in rule
    assert "not completed — " in rule and "no findings" in rule
    assert "AND output ? 'discovery_id'" not in source
    # And the blanket strip of caller-supplied adoption is still there.
    assert 'k.startswith("adopt_")' in source
