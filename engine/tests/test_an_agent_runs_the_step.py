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


def validate_captain_with_roster_refuses_a_typo() -> bool:
    from boss_engine.services.workflow_capabilities import validate_captain

    return bool(validate_captain({"id": "publish", "captain": "vidoe_editor"},
                                 roster={"video_editor", "release_auditor"}))


def test_a_misspelt_agent_is_caught_when_the_workflow_is_published():
    """Only the built-in capabilities had their captain checked.

    So a typo on a customer's step published cleanly and was denied at RUN time — twenty
    minutes into a workflow — as "caller 'vidoe_editor' not in allowed_callers".
    """
    source = inspect.getsource(workflows)
    # The call now passes the project's roster, so an agent the operator created is accepted
    # and a typo still is not (tests/test_a_created_agent_can_captain_a_step.py). Updated from
    # the one-argument form in review round 1 of milestone/agents; the check itself is unchanged.
    assert "captain_problems = validate_captain(step, roster)" in source
    assert validate_captain_with_roster_refuses_a_typo()

    from boss_engine.services.workflow_capabilities import validate_captain

    assert validate_captain({"id": "publish", "captain": "vidoe_editor"}), \
        "a role the workforce does not have must be refused"
    assert validate_captain({"id": "publish", "captain": "media_publisher"}) == []
    # No captain is not an error: it is the existing behaviour every workflow relies on.
    assert validate_captain({"id": "publish"}) == []


def test_the_builder_palette_endpoint_actually_runs():
    """CALLED, NOT PARSED.

    The roles list was added to this endpoint's payload and the import that made the name exist
    was not — an edit script raised between the two and wrote neither, then a second run wrote
    only the usage. `ast.parse` said "syntax OK" because a missing name is not a syntax error,
    the engine suite passed because it never imports this module's handlers, and the console
    tests are string comparisons. So a 500 on the endpoint that draws the entire builder
    palette shipped, and the first sign of it was an empty list on a live install.

    This test exists because the cheap check answered a different question than the one I asked.
    """
    import os

    os.environ.setdefault("BOSS_EDITION", "enterprise_self_hosted")
    from apps.api.app import main

    payload = main.list_delivery_capabilities(project_id=None)
    assert payload["capabilities"], "the palette must not be empty"
    assert payload["roles"], "a step must have agents to offer"
    assert "media_publisher" in payload["roles"]
    assert isinstance(payload["manual_approval_risk_floor"], int)


def test_an_agent_that_cannot_run_the_step_is_caught_when_the_workflow_is_saved():
    """Not twenty minutes into a run.

    The captain IS the permission identity, so assigning video_editor to a publish step is
    refused by the runtime — correctly, and at the worst possible moment: the operator has
    waited through a render to learn the last step could never have run. Both facts are known
    at save time.
    """
    source = inspect.getsource(steps.validate_step)
    assert "_allowed_callers(" in source
    assert "which is not " in source and "allowed to run" in source


def test_the_check_does_not_fail_closed_on_its_own_lookup():
    """A skill whose spec cannot be read here must not make every workflow using it
    unpublishable. The runtime performs the real check either way."""
    source = inspect.getsource(steps._allowed_callers)
    assert "return set()" in source
    assert "if callers and captain not in callers" in inspect.getsource(steps.validate_step), \
        "an empty set must mean 'do not judge', not 'refuse everything'"


def test_the_allowed_caller_lookup_actually_returns_callers(tmp_path):
    """EXERCISED, NOT READ.

    The first version of this guard passed `None` where a Settings was wanted. That threw in
    EntitlementBrokerService's constructor, the fail-open `except` swallowed it, and the check
    silently never ran — so assigning video_editor to a publish step saved cleanly, which is
    the exact thing the guard exists to refuse.

    Two tests asserted the source looked right and both passed. This one asks the function.
    """
    import os

    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        import pytest

        pytest.skip("no database configured")

    engine = create_engine(url)
    with engine.connect() as connection:
        callers = steps._allowed_callers(connection, "boss-dogfood", "publish_youtube")

    # An empty set means the guard silently does nothing, which is the failure being tested.
    assert callers, "the lookup returned nothing, so the guard would never fire"
    assert "media_publisher" in callers
    assert "video_editor" not in callers, "the whole separation depends on this"
