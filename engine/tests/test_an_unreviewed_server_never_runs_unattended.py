"""A tool on an MCP server nobody has reviewed never runs unattended — on ANY road to it.

THE INCIDENT (round-2 review of the skills-mcp milestone). Round 1 made an unreviewed server's
tool wait for a person, but only at one call site: agent_loop's direct MCP call. Probed on real
Postgres, a chat-registered server that a person turned on and lowered to tier 0 without
reviewing gave:

* an authored ``mcp_tool`` skill on it -> catalogue tier 0, ``requires_approval`` False, and an
  agent holding the skill ran it with nobody asked;
* a custom workflow step bound to the tool -> ``{risk_tier: 0, usable: True}``, an auto step;
* and the enforcing test was a source-string check on ``_drive_run``, blind to both.

The rule now lives in one place (mcp_registry.needs_person) and every gate asks it; the effect
itself (call_tool_with_receipt) refuses without an APPROVED decision, so a road that skips its
gate fails with a sentence instead of running. The tests below ENUMERATE the roads: each one is
driven unattended (refused, the server never called) and then with a person's approval (called
exactly once), and a scan fails if a new caller of the effect does not carry the approval.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_loop
from boss_engine.services import mcp_registry
from boss_engine.services import skill_catalogue as catalogue
from boss_engine.services import skill_definitions as sd
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services.core import create_blocking_decision
from boss_engine.services.mcp_client import MCPClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
TOOL = "create_issue"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT reachable FROM mcp_servers LIMIT 1"))
            c.execute(text("SELECT 1 FROM skill_definitions LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'review test', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        # Every table holding a project_id, until nothing refuses: the order they reference
        # each other in is the schema's business, not this test's.
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name='project_id' "
            "AND table_schema = current_schema() AND table_name <> 'projects'"))]
        for _ in range(4):
            for table in tables:
                try:
                    with c.begin_nested():
                        c.execute(text(f'DELETE FROM "{table}" WHERE project_id=:p'),
                                  {"p": pid})
                except Exception:  # noqa: BLE001 — referenced still; the next pass gets it
                    pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


@pytest.fixture()
def mcp_calls(monkeypatch):
    """What reached the server. The tool list is answered so the agent loop can offer it."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(MCPClient, "list_tools",
                        lambda self, url, headers=None: [{"name": TOOL, "inputSchema": {
                            "type": "object", "properties": {"title": {"type": "string"}}}}])
    monkeypatch.setattr(MCPClient, "call_tool",
                        lambda self, url, tool, args: calls.append((tool, args))
                        or {"issue": 42})
    return calls


def _settings() -> Settings:
    # Threshold 1, so a tier-0 tool is far below it: nothing but the review can ask a person.
    return Settings().model_copy(update={"agent_autonomous_risk_max": 1})


def _unreviewed_server(c, project) -> str:
    """The round-2 probe's server: registered from a chat (off, unreviewed), then turned on and
    lowered to tier 0 by a person who never marked it reviewed."""
    server = mcp_registry.register_mcp_server(
        c, project, "tracker", "http://tracker:9/mcp", risk_tier=2, actor="op", enabled=False)
    mcp_registry.update_mcp_server(c, project, server["id"], actor="op", enabled=True,
                                   risk_tier=0)
    mcp_registry.record_reachability(c, project, server["id"], reachable=True,
                                     tools=[{"name": TOOL}])
    assert mcp_registry.reviewed(mcp_registry.get_mcp_server(c, project, server["id"])) is False
    return str(server["id"])


def _skill_on(c, project, server_id: str) -> None:
    sd.save_skill(c, project_id=project, actor="op", skill_key="file_ticket",
                  label="File ticket", behaviour_kind="mcp_tool", risk_tier=0,
                  config={"mcp_server_id": server_id, "tool_name": TOOL})
    c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                   "VALUES (:i, :p, 'pm', 'file_ticket', 'authored')"),
              {"i": f"rs_{uuid.uuid4().hex[:10]}", "p": project})


class _Model:
    """Calls the one offered tool whose name contains `want`, then says it is done."""

    def __init__(self, want: str):
        self.want, self.turns, self.offered = want, 0, []

    def create_chat_completion(self, messages, tools=None):
        usage = {"model": "test", "prompt_tokens": 1, "completion_tokens": 1,
                 "total_tokens": 2}
        self.turns += 1
        if self.turns == 1:
            self.offered = [t["function"]["name"] for t in tools or []]
            name = next(n for n in self.offered if self.want in n)
            args = json.dumps({"title": "Broken build"})
            raw = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}]}
            return ({"raw": raw, "content": "",
                     "tool_calls": [{"id": "c1", "name": name, "arguments": args}]}, usage)
        return ({"raw": {"role": "assistant", "content": "done"}, "content": "done",
                 "tool_calls": []}, usage)


def _approve(c, decision_id: str) -> None:
    c.execute(text("UPDATE decisions SET status='approved', approved_by='ceo' WHERE id=:i"),
              {"i": decision_id})


def _a_decision(c, project, about: dict | None = None) -> str:
    return create_blocking_decision(c, project, None, "Approve the step", dict(about or {}),
                                    {}, "test")


#: What the custom-step road's gate decision is about: the run and step _step_road executes.
#: REWRITTEN in round 3: the helper used to approve a decision with an EMPTY recommendation and
#: the backstop accepted it, because it only asked "approved, in this project?". That was the
#: defect (an unrelated approval released the tool); the gate _open_gate really raises names
#: its workflow_run_id and step_id, so the test now raises that one.
STEP_GATE = {"workflow_run_id": "wfr_t", "step_id": "s1"}


# --------------------------------------------------------------------------- #
# The roads. Each takes (connection, project, server_id, approved) and returns  #
# the sentence it refused with, or None when it ran.                            #
# --------------------------------------------------------------------------- #

def _agent_road(want: str, finalize_ctx: dict):
    def road(c, project, server_id, approved, monkeypatch):
        monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _s: _Model(want))
        result = agent_loop.run_agent_loop(c, _settings(), project, "system", "File it",
                                           max_steps=2, finalize_ctx=finalize_ctx)
        run = agent_loop.load_agent_run(c, result["run_id"])
        if run["status"] != "awaiting_approval":
            return None
        if not approved:
            return json.dumps(run["messages"])
        _approve(c, run["pending"]["decision_id"])
        agent_loop.resume_agent_run(c, _settings(), result["run_id"])
        return None
    return road


def _step_road(binding: str):
    def road(c, project, server_id, approved, monkeypatch):
        key = f"{binding}_step"
        if custom_steps.get(c, project, key) is None:
            custom_steps.create(
                c, project_id=project, label=key, actor="op", step_key=key,
                binding_kind=binding,
                **({"skill_id": "file_ticket"} if binding == "skill"
                   else {"mcp_server_id": server_id, "mcp_tool_name": TOOL}))
        step = {"id": "s1", "kind": "capability", "binding": {"custom_step": key},
                "approval": {"mode": "auto"}}
        # The publish-time half: the step is manual whatever was asked for, and says why.
        definition = custom_steps.get(c, project, key)
        assert definition["approval_forced_manual"] is True
        assert "Reviewed" in (definition["requires_person"] or "")
        problems = custom_steps.validate_step(c, project, step)
        assert any("requires a person" in p and "review" in p for p in problems), problems
        try:
            custom_steps.execute(c, Settings(), project_id=project, actor="workflow",
                                 step=step, inputs={"title": "Broken build"}, run_id="wfr_t",
                                 approved_by=_approved_decision(c, project, STEP_GATE)
                                 if approved else None)
        except custom_steps.CustomStepError as error:
            return str(error)
        return None
    return road


def _approved_decision(c, project, about: dict | None = None) -> str:
    decision = _a_decision(c, project, about)
    _approve(c, decision)
    return decision


ROADS = {
    "an agent calls the tool directly": _agent_road("mcp_tracker", {}),
    "an agent holds an mcp_tool skill on the server": _agent_road(
        "skill_file_ticket", {"agent_step": {"role_key": "pm", "skill_ids": ["file_ticket"]}}),
    "a custom step is bound to the tool": _step_road("mcp_tool"),
    "a custom step is bound to an mcp_tool skill on the server": _step_road("skill"),
}


@pytest.mark.parametrize("road", sorted(ROADS))
def test_no_road_runs_an_unreviewed_servers_tool_unattended(eng, project, mcp_calls,
                                                           monkeypatch, road):
    """Round-2 review: only the direct agent call waited for a person; the skill and the custom
    step ran the same tool with nobody asked. Every road is held here, and says what to do."""
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        _skill_on(c, project, sid)
        refused = ROADS[road](c, project, sid, False, monkeypatch)
    assert mcp_calls == [], f"{road}: the unreviewed server was called with nobody asked"
    assert refused and "Skills and MCP" in refused, refused


@pytest.mark.parametrize("road", sorted(ROADS))
def test_with_a_persons_approval_every_road_runs_the_tool_once(eng, project, mcp_calls,
                                                              monkeypatch, road):
    """The rule asks a person; it does not forbid the tool. Approved, each road calls it once."""
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        _skill_on(c, project, sid)
        refused = ROADS[road](c, project, sid, True, monkeypatch)
    assert refused is None, refused
    assert [tool for tool, _ in mcp_calls] == [TOOL], road


def test_once_reviewed_a_tier_zero_tool_runs_without_asking(eng, project, mcp_calls,
                                                           monkeypatch):
    """Keep what worked: a reviewed server's tier-0 tool still runs unattended, on every road."""
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        mcp_registry.update_mcp_server(c, project, sid, actor="op", trusted=True)
        _skill_on(c, project, sid)
        for name in ("an agent calls the tool directly",
                     "an agent holds an mcp_tool skill on the server"):
            assert ROADS[name](c, project, sid, False, monkeypatch) is None, name
        for binding, target in (("mcp_tool", {"mcp_server_id": sid, "mcp_tool_name": TOOL}),
                                ("skill", {"skill_id": "file_ticket"})):
            key = f"{binding}_step"
            custom_steps.create(c, project_id=project, label=key, actor="op", step_key=key,
                                binding_kind=binding, **target)
            assert custom_steps.get(c, project, key)["approval_forced_manual"] is False
            custom_steps.execute(c, Settings(), project_id=project, actor="workflow",
                                 step={"id": "s", "binding": {"custom_step": key}},
                                 inputs={"title": "x"}, run_id="wfr_t")
    assert len(mcp_calls) == len(ROADS)


def test_the_catalogue_and_a_skill_step_say_a_person_is_needed(eng, project):
    """The SkillSpec an agent is offered, the catalogue entry every screen reads, and a
    workflow `kind: skill` step all carry the rule — the probe found requires_approval False."""
    from boss_engine.services import workflows

    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        _skill_on(c, project, sid)
        item = catalogue.entry(c, project, "file_ticket", settings=Settings())
        bindable = next(b for b in catalogue.bindable(c, project, settings=Settings())
                        if b["skill_id"] == "file_ticket")
        with pytest.raises(workflows.WorkflowBindingBlockedError) as refused:
            workflows._validate_bindings(c, project, "v_t", {"steps": [
                {"id": "s1", "kind": "skill", "binding": {"skill_id": "file_ticket"},
                 "approval": {"mode": "auto"}}]}, workflows.PRODUCT_NAMESPACE)
    assert item["risk_tier"] == 0 and "Reviewed" in item["requires_person"]
    assert item["spec"]["policy"]["requires_approval"] is True
    assert bindable["requires_approval"] is True
    assert "requires a person" in str(refused.value)


def _shipping_calls(attr: str):
    """Every call to ``<anything>.<attr>(...)`` or ``<attr>(...)`` in shipping code."""
    for top in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(
                        node.func, "id", getattr(node.func, "attr", None)) == attr:
                    yield rel, node


#: Clients allowed to call an MCP tool outside mcp_registry, and why. Each one's URL comes from
#: DEPLOYMENT CONFIGURATION (an environment variable the installer writes), never from the
#: mcp_servers registry an operator or a chat can edit — so the review rule, which is about
#: registry servers, has nothing to ask about them.
CONFIG_URL_CLIENTS = {
    "engine/boss_engine/services/mcp_repo_host.py":
        ("the repo host gateway URL is the BOSS_REPO_MCP_URL setting; repo_host.py builds "
         "it from settings only"),
    "engine/boss_engine/services/mcp_client.py": "the client itself",
    "engine/boss_engine/services/mcp_registry.py":
        "the two governed doors: call_tool_with_receipt and call_tool_unattended",
}


def test_no_shipping_code_calls_an_mcp_tool_around_the_registry():
    """Round-3 review: the meeting research phase called the registered 'web-intel' server
    with MCPClient.call_tool directly, so a re-pointed (unreviewed) web-intel received every
    meeting's URL with nobody asked — and the scan below could not see it, because it only
    listed callers of call_tool_with_receipt. This one fails on ANY `.call_tool(` in shipping
    code outside the registry and the named config-URL clients, so the next road to a
    registry server's tool has to go through needs_person."""
    stray = [f"{rel}:{node.lineno}" for rel, node in _shipping_calls("call_tool")
             if rel not in CONFIG_URL_CLIENTS]
    assert not stray, (
        "these call an MCP tool without mcp_registry (call_tool_with_receipt, or "
        f"call_tool_unattended where nobody can be asked): {stray}")
    # The allowlist stays honest: every entry still exists and still calls a tool.
    callers = {rel for rel, _ in _shipping_calls("call_tool")}
    assert set(CONFIG_URL_CLIENTS) - {"engine/boss_engine/services/mcp_client.py"} <= callers


def test_every_caller_of_the_mcp_effect_carries_the_approval():
    """The backstop only holds if the approval reaches it. Every call to
    call_tool_with_receipt in the shipping code passes `approved_by` AND `approval_about` (what
    the decision must be about — round 3), so a new road to an MCP tool cannot be written that
    drops the person's decision on the floor or accepts somebody else's."""
    missing, found = [], []
    for top in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(
                        node.func, "id", getattr(node.func, "attr", None)
                ) == "call_tool_with_receipt":
                    found.append(f"{rel}:{node.lineno}")
                    given = {k.arg for k in node.keywords}
                    if not {"approved_by", "approval_about"} <= given:
                        missing.append(f"{rel}:{node.lineno}")
    assert len(found) >= 2, found
    assert not missing, f"these reach an MCP tool without carrying the approval: {missing}"


# --------------------------------------------------------------------------- #
# What counts as reviewed                                                       #
# --------------------------------------------------------------------------- #

def test_a_repointed_or_unticked_builtin_is_unreviewed_everywhere(eng, project):
    """Round-2 review: a built-in counted as reviewed by provenance.source alone, so re-pointing
    a seeded 127.0.0.1 server at another host — the boss-1 case — or unticking Reviewed showed
    "unreviewed" on the panel while the agent loop ran its tools unattended."""
    builtin = {"source": "builtin", "version": "1", "trusted": True}
    with eng.begin() as c:
        seeded = mcp_registry.ensure_mcp_server(
            c, project, "web-intel", "http://127.0.0.1:8770/mcp", provenance=builtin,
            respect_operator=True)
        assert seeded["trusted"] is True and not mcp_registry.needs_person(seeded)

        moved = mcp_registry.update_mcp_server(c, project, seeded["id"], actor="op",
                                               url="http://evil.example/mcp")
        assert moved["trusted"] is False, "the panel"
        assert mcp_registry.needs_person(moved), "the agent loop, the catalogue, resolve_risk"
        assert moved["provenance"]["source"] == "builtin_edited"
        assert moved["provenance"]["seeded_url"] == "http://127.0.0.1:8770/mcp"
        # The next seed leaves the operator's server alone and does not restore the review.
        again = mcp_registry.ensure_mcp_server(
            c, project, "web-intel", "http://127.0.0.1:8770/mcp", provenance=builtin,
            respect_operator=True)
        assert again["url"] == "http://evil.example/mcp" and again["trusted"] is False

        other = mcp_registry.ensure_mcp_server(
            c, project, "ops", "http://127.0.0.1:8771/mcp", provenance=builtin,
            respect_operator=True)
        unticked = mcp_registry.update_mcp_server(c, project, other["id"], actor="op",
                                                  trusted=False)
        assert unticked["trusted"] is False and mcp_registry.needs_person(unticked)
        # A person reviewing it again is recorded as that person, not as the release.
        back = mcp_registry.update_mcp_server(c, project, other["id"], actor="cto",
                                              trusted=True)
    assert back["trusted"] is True and back["provenance"]["reviewed_by"] == "cto"
    # A legacy built-in row seeded before the flag existed is still the release's.
    assert mcp_registry.reviewed({"provenance": {"source": "builtin"}})
    assert not mcp_registry.reviewed({"provenance": {"source": "builtin", "trusted": False}})


def test_two_registrations_racing_for_one_name_get_the_sentence_not_a_500(eng, project,
                                                                          monkeypatch):
    """Round-2 review: the name check is check-then-insert, so the loser of a race hit the
    unique index and the API answered 500 with no explanation."""
    with eng.begin() as c:
        mcp_registry.register_mcp_server(c, project, "tracker", "http://a:1/mcp", actor="op")
        monkeypatch.setattr(mcp_registry, "_name_taken", lambda *a, **k: None)
        with pytest.raises(mcp_registry.McpServerConflict) as lost:
            mcp_registry.register_mcp_server(c, project, "Tracker", "http://b:1/mcp",
                                             actor="op")
        other = mcp_registry.register_mcp_server(c, project, "other", "http://c:1/mcp",
                                                 actor="op")
        with pytest.raises(mcp_registry.McpServerConflict):
            mcp_registry.update_mcp_server(c, project, other["id"], actor="op",
                                           name="TRACKER")
        # The transaction is still usable after both refusals.
        names = sorted(s["name"] for s in mcp_registry.list_mcp_servers(c, project))
    assert "already registered" in str(lost.value)
    assert names == ["other", "tracker"]


def test_a_publish_builds_the_catalogue_once_not_once_per_step(eng, project, monkeypatch):
    """Round-2 review: every skill step re-read and re-parsed every skill YAML, so a workflow
    with N skill steps built the whole catalogue N times at publish."""
    from boss_engine.services.entitlements import EntitlementBrokerService

    builds = []
    real = EntitlementBrokerService.list_skills

    def counted(self, *a, **k):
        builds.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(EntitlementBrokerService, "list_skills", counted)
    with eng.begin() as c:
        with catalogue.one_pass(c):
            for _ in range(5):
                catalogue.entry(c, project, "model.generate_text", settings=Settings())
        assert len(builds) == 1
        # Outside a pass every read is fresh, so a write is seen by the next read.
        sd.save_skill(c, project_id=project, actor="op", skill_key="later", label="Later",
                      behaviour_kind="builtin", config={"executor_id": "model.generate_text"})
        before = len(builds)
        assert catalogue.entry(c, project, "later", settings=Settings()) is not None
    assert len(builds) == before + 1


# --------------------------------------------------------------------------- #
# Round 3: the roads the backstop could not see, and what the approval is for   #
# --------------------------------------------------------------------------- #

def test_a_meeting_never_sends_its_url_to_an_unreviewed_web_intel(eng, project, mcp_calls):
    """Round-3 review, probed on Postgres: the built-in web-intel re-pointed at
    http://elsewhere.example/mcp turned unreviewed, and the meeting's research phase still
    called it — `_gather_research` gave calls=[('http://elsewhere.example/mcp', 'study_site')]
    and the reply went into every persona prompt. The untouched seeded server keeps working;
    a re-pointed one, or a person-registered 'web-intel' nobody reviewed, is skipped and the
    research note says why and what to do, so the meeting still runs."""
    from boss_engine.services import meeting_service

    builtin = {"source": "builtin", "version": "1", "trusted": True}
    with eng.begin() as c:
        seeded = mcp_registry.ensure_mcp_server(
            c, project, "web-intel", "http://127.0.0.1:8770/mcp", provenance=builtin,
            respect_operator=True)
        kept = meeting_service._gather_research(c, Settings(), project, "https://example.com")
        assert [tool for tool, _ in mcp_calls] == ["study_site"], "the reviewed seed stopped"
        assert "pages" in kept and "research skipped" not in str(kept.get("note"))

        mcp_registry.update_mcp_server(c, project, seeded["id"], actor="op",
                                       url="http://elsewhere.example/mcp")
        moved = meeting_service._gather_research(c, Settings(), project, "https://example.com")

        # Deleted and registered again under the same name by a person, never reviewed.
        mcp_registry.delete_mcp_server(c, project, seeded["id"], actor="op")
        mcp_registry.register_mcp_server(c, project, "web-intel", "http://other.example/mcp",
                                         actor="op", enabled=True)
        replaced = meeting_service._gather_research(c, Settings(), project,
                                                    "https://example.com")
    assert [tool for tool, _ in mcp_calls] == ["study_site"], (
        "an unreviewed web-intel was sent the meeting's URL with nobody asked")
    for research in (moved, replaced):
        assert research["pages"] == []
        assert "research skipped" in research["note"]
        assert "Reviewed" in research["note"] and "Skills and MCP" in research["note"]


def test_an_approval_about_another_step_does_not_release_the_tool(eng, project, mcp_calls):
    """Round-3 review: the backstop accepted ANY approved decision in the project. Now the
    decision has to be about this run and step; another step's approval, another run's, or an
    agent run's are refused with a sentence saying which approval to give."""
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        server = mcp_registry.get_mcp_server(c, project, sid)
        assert mcp_registry.approval_stands(
            c, project, _approved_decision(c, project, STEP_GATE), STEP_GATE)
        wrong = None
        for about in ({"workflow_run_id": "wfr_t", "step_id": "other"},
                      {"workflow_run_id": "wfr_other", "step_id": "s1"},
                      {"run_id": "arun_x"}, {}):
            wrong = _approved_decision(c, project, about)
            assert not mcp_registry.approval_stands(c, project, wrong, STEP_GATE), about
        # The right subject but nobody has answered yet: still not an approval.
        pending = _a_decision(c, project, STEP_GATE)
        assert not mcp_registry.approval_stands(c, project, pending, STEP_GATE)
        # Naming nothing to match is never an approval.
        assert not mcp_registry.approval_stands(
            c, project, _approved_decision(c, project, STEP_GATE), None)
        # A blank field is not a wildcard: a step with no run id matches no run's gate.
        assert not mcp_registry.approval_stands(
            c, project, _approved_decision(c, project, {"step_id": "s1"}),
            {"workflow_run_id": "", "step_id": "s1"})

        with pytest.raises(mcp_registry.McpServerUnreviewed) as other:
            mcp_registry.call_tool_with_receipt(
                c, Settings(), project, server, TOOL, {}, dedupe_key="k1",
                tenant_scope=("org", "ws"), approved_by=wrong, approval_about=STEP_GATE)
        with pytest.raises(mcp_registry.McpServerUnreviewed) as none:
            mcp_registry.call_tool_with_receipt(
                c, Settings(), project, server, TOOL, {}, dedupe_key="k2",
                tenant_scope=("org", "ws"), approved_by=None, approval_about=None)
    assert mcp_calls == []
    assert "not an approved decision about this run or step" in str(other.value)
    # A road that carries no approval (a workflow `kind: skill` step's skill.execute job) is
    # told the fix that works there, not "approve it when BOSS asks".
    assert "approving does not release it here" in str(none.value)
    assert "mark the server Reviewed" in str(none.value)


def test_an_unreviewed_refusal_is_recorded_as_one_attempt(eng, project, mcp_calls):
    """Round-3 review: SkillRuntimeService retried the refusal under the MCP spec's retry
    policy (max 2), so the run recorded a second attempt at a call only a person could
    release. The refusal is not retryable."""
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        custom_steps.create(c, project_id=project, label="tool_step", actor="op",
                            step_key="tool_step", binding_kind="mcp_tool",
                            mcp_server_id=sid, mcp_tool_name=TOOL)
        with pytest.raises(custom_steps.CustomStepError):
            custom_steps.execute(c, Settings(), project_id=project, actor="workflow",
                                 step={"id": "s1", "binding": {"custom_step": "tool_step"}},
                                 inputs={"title": "x"}, run_id="wfr_t")
        row = c.execute(text("SELECT status, attempts, max_attempts FROM skill_invocations "
                             "WHERE project_id=:p ORDER BY created_at DESC LIMIT 1"),
                        {"p": project}).mappings().one()
    assert mcp_calls == []
    assert row["status"] == "failed" and row["max_attempts"] >= 2
    assert row["attempts"] == 1, "the refusal was retried"


def test_a_legacy_timeout_approval_never_releases_an_unreviewed_step(eng, project, mcp_calls,
                                                                     monkeypatch):
    """Round-3 review: a custom step published with on_timeout=approve (legal while its server
    was reviewed at tier 0) kept that in its gate snapshot when the server became unreviewed
    while a run was under way.
    The gate is forced manual, but the timeout sweep read on_timeout=approve and approved it BY
    POLICY. The gate now opens with on_timeout=block and the reason, and the sweep blocks."""
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services import workflows

    class _NoTools:
        def specs(self):
            return []

    class _Ctx:
        def begin(self):
            return eng.begin()

    monkeypatch.setattr(runner, "_tool_registry", lambda: _NoTools())
    with eng.begin() as c:
        sid = _unreviewed_server(c, project)
        mcp_registry.update_mcp_server(c, project, sid, actor="op", trusted=True)
        custom_steps.create(c, project_id=project, label="File it", actor="op",
                            step_key="file_it", binding_kind="mcp_tool",
                            mcp_server_id=sid, mcp_tool_name=TOOL)
        definition = workflows.create_definition(
            c, project_id=project, workflow_key="legacy_sla", name="legacy", description=None,
            actor="test", namespace="product")
        version = workflows.publish_version(
            c, project_id=project, definition_id=definition["id"], actor="test",
            spec={"entry_step": "s1", "steps": [
                {"id": "s1", "kind": "capability", "next": [],
                 "approval": {"mode": "manual", "sla_hours": 1, "on_timeout": "approve"},
                 "binding": {"custom_step": "file_it", "inputs": {}}}]})
        run = workflows.start_run(c, project_id=project, actor="test", input_data={},
                                  dedupe_key=f"dk_{uuid.uuid4().hex[:10]}",
                                  workflow_version_id=version["id"])
        # The server stops being reviewed AFTER the run started (a new start is refused at
        # _validate_bindings; a run already under way reaches the gate).
        mcp_registry.update_mcp_server(c, project, sid, actor="op", trusted=False)
        runner.tick(c, _Ctx(), project_id=project)
        snapshot = c.execute(text("SELECT binding_snapshot FROM workflow_step_runs "
                                  "WHERE workflow_run_id=:r"), {"r": run["id"]}).scalar()
        c.execute(text("UPDATE workflow_step_runs SET binding_snapshot = binding_snapshot || "
                       "jsonb_build_object('due_at', (now() - interval '1 hour')::text) "
                       "WHERE workflow_run_id=:r"), {"r": run["id"]})
        swept = runner.sweep_gate_timeouts(c, project_id=project)
        status = c.execute(text("SELECT status FROM workflow_step_runs "
                                "WHERE workflow_run_id=:r"), {"r": run["id"]}).scalar()
    assert snapshot["approval"]["on_timeout"] == "block"
    assert "Reviewed" in (snapshot.get("requires_person") or "")
    assert swept["applied"][0]["outcome"] == "blocked"
    assert status == "waiting" and mcp_calls == []
