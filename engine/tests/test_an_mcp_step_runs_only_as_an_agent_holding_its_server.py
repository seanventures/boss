"""A custom step bound to an MCP tool runs only as an agent that holds the server.

THE INCIDENT (independent review of milestone/integration, blocker). The last round closed the
MCP reach hole for skills, but a custom step bound DIRECTLY to an MCP tool that no agent held
still ran as `workflow_runner`, in current versions too: `step_agents.who_may_run` returned
WORKFLOW_RUNNER as an unconditional fallback for every MCP-tool step, the synthesised MCP spec
lists `workflow_runner` among its callers, and the runtime's server gate only looked at
authored `mcp_tool` skills. The chat model can author exactly this with no human click —
`draft_custom_step` and `draft_workflow` are `safe`, and draft_workflow can REPLACE the steps of
an existing workflow and publish them as its active version — and a schedule then runs that
version unattended. A reviewed server's tool, with its credentials, ran without anybody ever
granting that server to anyone.

THE RULE (agents.mcp_grant_gap, the one grant rule): a step can only narrow what its agent
already holds. A step naming nobody is given to an agent holding the server, or refused at
publish with the sentence that says what to do; a step naming an agent is checked against the
server at publish too. Only a version published under the old rules still runs as the workflow
(`legacy_workflow_runner`) — the one named road in agents.SERVERLESS_MCP_GRANTS.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_loop
from boss_engine.services import agents
from boss_engine.services import mcp_registry
from boss_engine.services import step_agents
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services import workflow_schedules
from boss_engine.services import workflows
from boss_engine.services.mcp_client import MCPClient
from boss_engine.tools.builtin import build_default_registry

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
TOOL = "post_message"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM role_mcp_servers LIMIT 1"))
            c.execute(text("SELECT 1 FROM workflow_schedules LIMIT 1"))
            if c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar() is None:
                pytest.skip("test database has no workspace")
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'mcp step', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name='project_id' "
            "AND table_schema = current_schema() AND table_name <> 'projects'"))]
        for _ in range(4):
            for table in tables:
                try:
                    with c.begin_nested():
                        c.execute(text(f'DELETE FROM "{table}" WHERE project_id=:p'),
                                  {"p": pid})
                except Exception:  # noqa: BLE001 — still referenced; the next pass gets it
                    pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


@pytest.fixture()
def calls(monkeypatch):
    """Every call that reached the MCP server. The subject of every test here is this list."""
    made: list[tuple[str, dict]] = []
    monkeypatch.setattr(MCPClient, "list_tools", lambda self, url, headers=None: [
        {"name": TOOL, "inputSchema": {"type": "object"}}])
    monkeypatch.setattr(MCPClient, "call_tool",
                        lambda self, url, tool, args: made.append((tool, args)) or {"ok": 1})
    return made


class _Ctx:
    settings = Settings()

    def __init__(self, engine):
        self._engine = engine

    @contextmanager
    def begin(self):
        with self._engine.begin() as c:
            yield c


def _server(c, project) -> str:
    """A REVIEWED tier-0 server, so nothing but the grant can stop the call, and an agent
    `publisher` that holds nothing yet."""
    server = mcp_registry.register_mcp_server(
        c, project, "mattermost", "http://mm:9/mcp", risk_tier=0, actor="op", enabled=True)
    mcp_registry.update_mcp_server(c, project, server["id"], actor="op", trusted=True)
    mcp_registry.record_reachability(c, project, server["id"], reachable=True,
                                     tools=[{"name": TOOL}])
    agents.create_agent(c, project, actor="op", name="Publisher", role_key="publisher")
    return str(server["id"])


def _mcp_step(c, project, sid, key="announce_it"):
    custom_steps.create(c, project_id=project, label="Announce it", actor="op", step_key=key,
                        binding_kind="mcp_tool", mcp_server_id=sid, mcp_tool_name=TOOL)


def _publish(c, project, workflow_key, step):
    definition = workflows.create_definition(
        c, project_id=project, workflow_key=workflow_key, name=workflow_key, description=None,
        actor="op", namespace="product")
    return definition, workflows.publish_version(
        c, project_id=project, definition_id=definition["id"], actor="op",
        spec={"entry_step": "s1", "steps": [{"id": "s1", "kind": "capability", "next": [],
                                             "approval": {"mode": "auto"}, **step}]})


def _drafted(step_id):
    """How the chat names a custom step in draft_workflow."""
    return {"id": step_id, "capability": "custom", "skill": "announce_it"}


def _revoke(c, project, role="publisher"):
    c.execute(text("DELETE FROM role_mcp_servers WHERE project_id=:p AND role_key=:r"),
              {"p": project, "r": role})


# ── Publish: the chat road, and the API road through the same publish ───────────────────────


def test_the_chat_cannot_publish_an_mcp_step_that_no_agent_holds(eng, project, calls):
    """The blocker's road: draft_custom_step on the server, then draft_workflow with it — both
    `safe`, both run by the model. The publish is refused with what to do, nothing is written,
    and the moment an agent holds the server the same draft publishes."""
    with eng.begin() as c:
        sid = _server(c, project)
    registry = build_default_registry()

    def call(tool, **args):
        return registry.get(tool).handler(_Ctx(eng), project_id=project, actor="ceo", args=args)

    drafted = call("draft_custom_step", step_key="announce_it", label="Announce it",
                   mcp_server="mattermost", mcp_tool=TOOL)
    assert drafted["status"] == "defined", drafted
    refused = call("draft_workflow", workflow_key="announcer", name="Announcer",
                   steps=[_drafted("s1")])
    assert refused["ok"] is False, refused
    assert "no agent holds MCP server 'mattermost'" in refused["error"], refused
    assert "Agents page" in refused["error"]
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM workflow_versions WHERE project_id=:p"),
                         {"p": project}).scalar() == 0

    with eng.begin() as c:
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
    published = call("draft_workflow", workflow_key="announcer", name="Announcer",
                     steps=[_drafted("s1")])
    assert published.get("status") == "published", published
    assert calls == []


def test_the_chat_cannot_replace_a_workflows_steps_with_an_mcp_step_nobody_holds(eng, project,
                                                                               calls):
    """draft_workflow REPLACES an existing workflow's steps and makes them its active version —
    the version a schedule runs. Replacing them with an unheld MCP step is refused and the
    active version stays the one that was there."""
    with eng.begin() as c:
        sid = _server(c, project)
        _mcp_step(c, project, sid)
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
    tool = build_default_registry().get("draft_workflow")
    made = tool.handler(_Ctx(eng), project_id=project, actor="ceo",
                        args={"workflow_key": "digest", "name": "Digest",
                              "steps": [_drafted("s1")]})
    assert made.get("status") == "published", made
    with eng.begin() as c:
        _revoke(c, project)
        active = c.execute(text("SELECT active_version_id FROM workflow_definitions "
                                "WHERE project_id=:p AND workflow_key='digest'"),
                           {"p": project}).scalar()
    again = tool.handler(_Ctx(eng), project_id=project, actor="ceo",
                         args={"workflow_key": "digest", "name": "Digest",
                               "steps": [_drafted("s1"),
                                         _drafted("s2")]})
    assert again["ok"] is False and "no agent holds MCP server 'mattermost'" in again["error"]
    with eng.begin() as c:
        assert c.execute(text("SELECT active_version_id FROM workflow_definitions "
                              "WHERE project_id=:p AND workflow_key='digest'"),
                         {"p": project}).scalar() == active
    assert calls == []


def test_a_named_agent_without_the_server_is_refused_at_publish_with_what_to_do(eng, project):
    """Minor from the same review: a step NAMING an agent was never checked against what it
    runs, so it published and failed at run time with "caller 'x' not in allowed_callers"."""
    with eng.begin() as c:
        sid = _server(c, project)
        _mcp_step(c, project, sid)
        with pytest.raises(workflows.WorkflowBindingBlockedError) as refused:
            _publish(c, project, "named", {"captain": "publisher",
                                           "binding": {"custom_step": "announce_it"}})
        said = str(refused.value)
        assert "publisher may not run" in said and "mattermost" in said, said
        assert "Agents page" in said and "allowed_callers" not in said

        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
        _, version = _publish(c, project, "named2", {"captain": "publisher",
                                                     "binding": {"custom_step": "announce_it"}})
        assert version["id"]


# ── Run time: the schedule, the gate, and the one legacy road ───────────────────────────────


def test_a_scheduled_run_never_calls_an_mcp_tool_that_no_agent_holds(eng, project, calls,
                                                                    monkeypatch):
    """The unattended road: a published version whose server was taken from every agent, fired
    by a schedule. Before the fix it started and the step ran as the workflow, calling the tool.
    Now the start is refused with the reason — and in no case does the tool run."""

    class _NoTools:
        def specs(self):
            return []

    monkeypatch.setattr(runner, "_tool_registry", lambda: _NoTools())
    with eng.begin() as c:
        sid = _server(c, project)
        agents.grant_mcp(c, project, actor="op", role_key="publisher", mcp_server_id=sid)
        _mcp_step(c, project, sid)
        _publish(c, project, "nightly", {"binding": {"custom_step": "announce_it"}})
        _revoke(c, project)
        schedule = workflow_schedules.create(
            c, project_id=project, workflow_key="nightly", name="nightly", cadence="hourly",
            minute=0, actor="ceo")
        c.execute(text("UPDATE workflow_schedules SET next_run_at=:t WHERE id=:i"),
                  {"t": datetime.now(timezone.utc) - timedelta(minutes=1), "i": schedule["id"]})
        fired = workflow_schedules.run_due(c, project_id=project)
    with eng.begin() as c:
        runner.tick(c, _Ctx(eng), project_id=project)
        runner.tick(c, _Ctx(eng), project_id=project)
        row = c.execute(text("SELECT last_error FROM workflow_schedules WHERE id=:i"),
                        {"i": schedule["id"]}).mappings().one()
    assert calls == [], "a schedule called a server nobody was given"
    assert fired and "Agents page" in (row["last_error"] or "") + str(fired), (fired, row)


def test_the_runtime_refuses_the_workflow_a_server_except_on_the_legacy_road(eng, project,
                                                                             calls):
    """The gate checks any spec that names a server, not only an authored mcp_tool skill — so
    the synthesised spec's `workflow_runner` caller no longer reaches the tool by itself."""
    with eng.begin() as c:
        sid = _server(c, project)
        server = mcp_registry.get_mcp_server(c, project, sid)
        denied = agent_loop._invoke_mcp_skill(
            c, Settings(), project, server, {"name": TOOL}, {"text": "x"},
            run_id="wfr_x", step=0, actor="op", caller="workflow_runner")
        assert denied["status"] == "denied", denied
        assert "workflow holds no MCP server" in denied["reason"]
        assert "Agents page" in denied["reason"]
        assert calls == []
        released = agent_loop._invoke_mcp_skill(
            c, Settings(), project, server, {"name": TOOL}, {"text": "x"},
            run_id="wfr_y", step=0, actor="op", caller="workflow_runner",
            mcp_reach="legacy_workflow_runner")
        assert released["status"] == "succeeded", released
        # The road is bound to its caller: an agent cannot borrow the legacy road.
        borrowed = agent_loop._invoke_mcp_skill(
            c, Settings(), project, server, {"name": TOOL}, {"text": "y"},
            run_id="wfr_z", step=0, actor="op", caller="publisher",
            mcp_reach="legacy_workflow_runner")
        assert borrowed["status"] == "denied", borrowed
    assert [tool for tool, _ in calls] == [TOOL]


def test_a_version_published_under_the_old_rules_still_runs_its_mcp_step_as_the_workflow(
        eng, project, calls):
    """Keep what worked: an unstamped version's MCP step nobody holds runs as it always did,
    and the run says how to move it to an agent. A current version's is refused at run time
    with the same sentence publish uses."""
    with eng.begin() as c:
        sid = _server(c, project)
        _mcp_step(c, project, sid)
        legacy = {"id": "s1", "binding": {"custom_step": "announce_it"},
                  step_agents.AGENT_RULES_KEY: step_agents.LEGACY_AGENT_RULES}
        out = custom_steps.execute(c, Settings(), project_id=project, actor="ceo", step=legacy,
                                   inputs={"text": "hi"}, run_id="wfr_legacy")
        assert out["ran_as"]["how"] == "legacy_workflow_runner", out["ran_as"]
        assert "Agents page" in out["ran_as"]["why"]

        current = {**legacy, step_agents.AGENT_RULES_KEY: step_agents.CURRENT_AGENT_RULES}
        with pytest.raises(custom_steps.CustomStepError) as refused:
            custom_steps.execute(c, Settings(), project_id=project, actor="ceo", step=current,
                                 inputs={"text": "again"}, run_id="wfr_current")
        assert "no agent holds MCP server 'mattermost'" in str(refused.value)
    assert [args for _, args in calls] == [{"text": "hi"}]


def test_a_named_agent_whose_server_was_taken_is_told_what_to_do_when_it_runs(eng, project,
                                                                              calls):
    """A grant can be taken away between publish and run. The runtime's refusal is the grant
    rule's sentence, naming the Agents page — not the raw allowed_callers string."""
    with eng.begin() as c:
        _mcp_step(c, project, _server(c, project))
        with pytest.raises(custom_steps.CustomStepError) as refused:
            custom_steps.execute(c, Settings(), project_id=project, actor="ceo",
                                 step={"id": "s1", "captain": "publisher",
                                       "binding": {"custom_step": "announce_it"}},
                                 inputs={"text": "x"}, run_id="wfr_named")
    said = str(refused.value)
    assert "publisher does not hold that server" in said and "Agents page" in said, said
    assert "allowed_callers" not in said
    assert calls == []
