"""A step that names a captain runs as that captain.

"we have added support to connect to the youtube channel, but that channel needs to be
associated to a publisher agent, then that agent should be added to workflow step"

Most of that existed. A step could name a captain, validate_captain refused one the workforce
does not have, the API read it and the console showed it as "the captain this WORKFLOW named
for this step, overriding the capability's default".

And the runner never read it. So a workflow saying "the publisher agent runs this step" was
recorded, validated, displayed — and the tool ran as whoever started the run. That is the whole
point of naming one: an agent holds the connection, the knowledge and the standing to act, and
a step that names the publisher and then publishes as the operator has authorised nothing and
attributed the act to the wrong party.
"""

from __future__ import annotations

import inspect

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import validate_captain


def test_the_runner_reads_the_step_captain():
    src = inspect.getsource(runner)
    assert 'step_actor = str(step.get("captain")' in src


def test_the_named_captain_is_who_the_tool_runs_as():
    src = inspect.getsource(runner)
    assert "_invoke_tool(ctx, capability.tool, project_id, step_actor, inputs)" in src
    # The run's own actor must no longer be passed straight through at that call.
    assert "_invoke_tool(ctx, capability.tool, project_id, actor, inputs)" not in src


def test_a_step_naming_nobody_still_runs_as_the_run_did():
    """Almost every step names no captain, and none of them may change behaviour."""
    src = inspect.getsource(runner)
    assert 'or actor' in src.split("step_actor =")[1].split("\n")[0]


def test_a_captain_the_workforce_does_not_have_is_refused():
    """Unchanged, and it is what stops this becoming a way to act as an invented role."""
    assert validate_captain({"id": "publish", "captain": "publisher_of_nowhere"})
    assert validate_captain({"id": "publish"}) == []


def test_the_name_is_normalised_the_same_way_validation_normalises_it():
    """validate_captain lowercases and strips before checking, so execution must too — or a
    step could pass validation and then act as a role that does not exist."""
    src = inspect.getsource(runner)
    line = src.split("step_actor =")[1].split("\n")[0]
    assert ".strip().lower()" in line
