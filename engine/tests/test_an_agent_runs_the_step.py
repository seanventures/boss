"""A step names an agent, and the agent runs it.

BOSS's own pipelines read as agents doing the work because a capability step's `captain`
becomes the actor. A CUSTOM step — the only kind a customer can build — ignored it and ran as
`workflow_runner` every time, so a customer's workflow had no agent in it anywhere. That is
the asymmetry: what BOSS does for itself, and what it lets a customer do.
"""
import inspect

from boss_engine.services import workflow_custom_steps as steps
from boss_engine.services import workflows


def test_a_custom_step_runs_as_the_agent_it_names():
    source = inspect.getsource(steps.execute)
    assert 'captain = str(step.get("captain")' in source
    # The captain is both the permission identity and the actor on the record: a step that
    # publishes as the publisher must not be attributed to whoever started the run.
    assert "captain or RUNNER_CALLER" in source
    assert "actor=captain or actor" in source


def test_naming_an_agent_is_not_a_way_to_grant_it_something():
    """THE POINT OF NAMING ONE.

    Without a captain the runner is added to the skill's allowed callers, because choosing the
    skill for a step IS the grant. With one, the ROLE has to be permitted on its own merits —
    so `video_editor` on a publish step is refused, because that role is forbidden to publish.
    Auto-granting whatever captain was typed would make the assignment decorative, which is
    worse than not having it at all.
    """
    source = inspect.getsource(steps.execute)
    assert "skill_spec=None if captain else _spec_granting_the_runner(" in source


def test_the_anonymous_runner_still_works_exactly_as_before():
    """Every workflow that exists names no captain, and none of them may change behaviour."""
    source = inspect.getsource(steps.execute)
    # The grant, and the runner caller, both survive for the no-captain path.
    assert "RUNNER_CALLER" in source and "_spec_granting_the_runner(" in source
    assert steps.RUNNER_CALLER == "workflow_runner"


def test_a_misspelt_agent_is_caught_when_the_workflow_is_published():
    """Only the built-in capabilities had their captain checked.

    So a typo on a customer's step published cleanly and was denied at RUN time — twenty
    minutes into a workflow — as "caller 'vidoe_editor' not in allowed_callers".
    """
    source = inspect.getsource(workflows)
    assert "captain_problems = validate_captain(step)" in source

    from boss_engine.services.workflow_capabilities import validate_captain

    assert validate_captain({"id": "publish", "captain": "vidoe_editor"}), \
        "a role the workforce does not have must be refused"
    assert validate_captain({"id": "publish", "captain": "media_publisher"}) == []
    # No captain is not an error: it is the existing behaviour every workflow relies on.
    assert validate_captain({"id": "publish"}) == []
