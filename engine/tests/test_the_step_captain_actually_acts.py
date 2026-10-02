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

REWRITTEN AS BEHAVIOUR. These tests used to assert that lines like `step_actor =
str(step.get("captain")` appeared in the runner's source, which is how a resolver that never
ran its model pick passed review. They now run `execute_step` against a recording tool and look
at who the tool was called as, and what the step recorded about it.
"""

from __future__ import annotations

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_capabilities import validate_captain


class _Tool:
    def __init__(self, name):
        self.name, self.kind, self.description, self.parameters = name, "read", name, {}
        self.calls: list[dict] = []

    def handler(self, ctx, *, project_id, actor, args):
        self.calls.append({"actor": actor, "args": dict(args)})
        return {"spend_usd": 0}


class _Registry:
    def __init__(self, tool):
        self._tool = tool

    def specs(self):
        return [self._tool]


class _Ctx:
    settings = None


def _run(monkeypatch, step):
    tool = _Tool("get_costs")
    monkeypatch.setattr(runner, "_tool_registry", lambda: _Registry(tool))
    output = runner.execute_step(None, _Ctx(), project_id="p", actor="ceo", step=step,
                                 context={"input": {}, "steps": {}})
    return tool, output


def _cost_check(**extra):
    return {"id": "cost", "kind": "capability",
            "binding": {"capability": "cost_check", "inputs": {}}, **extra}


def test_the_named_captain_is_who_the_tool_runs_as(monkeypatch):
    tool, output = _run(monkeypatch, _cost_check(captain="finops_sentinel"))
    assert tool.calls[0]["actor"] == "finops_sentinel"
    assert output["ran_as"]["agent"] == "finops_sentinel"
    assert output["ran_as"]["how"] == "named"


def test_a_step_naming_nobody_still_runs_as_the_run_did(monkeypatch):
    """Almost every step names no captain — all twelve catalogue workflows — and none of them
    may change behaviour. What changed is that the step now SAYS so, with the reason."""
    tool, output = _run(monkeypatch, _cost_check())
    assert tool.calls[0]["actor"] == "ceo"
    assert output["ran_as"]["how"] == "run_actor"
    assert "needs no grant" in output["ran_as"]["why"]
    assert "ceo" in output["ran_as"]["why"]


def test_the_name_is_normalised_the_same_way_validation_normalises_it(monkeypatch):
    """validate_captain lowercases and strips before checking, so execution must too — or a
    step could pass validation and then act as a role that does not exist."""
    tool, _ = _run(monkeypatch, _cost_check(captain="  FinOps_Sentinel "))
    assert tool.calls[0]["actor"] == "finops_sentinel"


def test_a_captain_the_project_does_not_have_is_refused():
    """Unchanged in intent — it is what stops this becoming a way to act as an invented role —
    but checked against the PROJECT's agents rather than a constant."""
    assert validate_captain({"id": "publish", "captain": "publisher_of_nowhere"},
                            roster=["media_publisher"])
    assert validate_captain({"id": "publish"}, roster=["media_publisher"]) == []
