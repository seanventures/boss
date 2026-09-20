"""What the panel decided must reach the agent that builds it.

Standard Product Delivery states its own objective in these words: "The panel's PRD is the
build brief: it is handed straight to an engineering agent, so it must be specific enough to
implement and to test against."

It was not handed to anyone. `build` bound `instructions` to the run's original one-line
directive, so the interview-scorecard build received 745 characters — the operator's sentence
plus deployability boilerplate — after 7m49s of research and a 22m48s panel whose UX role had
specified behavioural anchors per rating, a visible rubric, and per-interviewer submission
status. The agent built precisely what its brief described: an API with no screen. No model
could have done otherwise; nothing it could see asked for one.

This is the same wiring bug as the one already fixed one step earlier, recorded in the
catalogue at the meeting step — "the panel that followed it debated the original one-line
directive as though none of it had happened". Research→meeting was wired then. Meeting→build
was not.
"""

from __future__ import annotations

from boss_engine.services import workflow_catalogue as wc
from boss_engine.services.workflow_capabilities import CAPABILITIES


def _steps(workflow: dict) -> dict:
    return {s["id"]: s for s in workflow["steps"]}


def _binds(step: dict) -> dict:
    """What a step binds. Lives under `binding.inputs`, not on the step itself."""
    return ((step.get("binding") or {}).get("inputs") or {})


def _workflows() -> list[dict]:
    return list(wc.CATALOGUE)


def test_the_build_capability_accepts_a_spec():
    build = CAPABILITIES["build"]
    assert "spec_markdown" in build.optional_inputs
    # Optional, never required: a build with no panel behind it must still run.
    assert "spec_markdown" not in build.required_inputs


def test_the_meeting_still_publishes_the_prd_this_depends_on():
    assert "prd_markdown" in CAPABILITIES["meeting"].produces


def test_every_build_that_follows_a_panel_is_given_its_prd():
    checked = 0
    for workflow in _workflows():
        steps = _steps(workflow)
        if "meeting" not in steps or "build" not in steps:
            continue
        binding = _binds(steps["build"])
        assert "spec_markdown" in binding, (
            f"{workflow['key']}: holds a panel and then builds without its PRD")
        checked += 1
    assert checked >= 8, f"expected the catalogue's panel-led workflows, saw {checked}"


def test_a_build_with_no_panel_is_left_alone():
    """fast_fix and security_hardening have no meeting: binding a PRD they never produce
    would fail the run outright."""
    seen = 0
    for workflow in _workflows():
        steps = _steps(workflow)
        if "build" in steps and "meeting" not in steps:
            assert "spec_markdown" not in _binds(steps["build"]), workflow["key"]
            seen += 1
    assert seen >= 1


def test_the_directive_is_never_replaced_by_the_spec():
    """Both reach the builder. The directive is what was asked for; the spec is how."""
    for workflow in _workflows():
        steps = _steps(workflow)
        if "build" in steps:
            assert "instructions" in _binds(steps["build"]), workflow["key"]
