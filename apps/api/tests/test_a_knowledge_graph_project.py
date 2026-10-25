"""A KNOWLEDGE-GRAPH PROJECT: chosen at creation, set up in steps, the same for a press and an ask.

The operator (BOSSAPP-768 follow-up): "there's no project type… for Knowledge graph how does the
project get created and imported?" A project now has a `project_type` — `app` or
`knowledge_graph` — set at POST /v1/projects (and by the agent's create_project), visible in
GET /v1/projects, describe_project and list_projects. A knowledge-graph project starts with its
graph switched on, and GET /v1/knowledge/setup says where its setup is: import code → assign the
team → first scan → review, each step derived from the records its own service writes (so it is
resumable from any screen or any agent). The team step is POST /v1/projects/{id}/agents
action=set, the same service as the agent's set_project_team. Real database, real authz.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.services.knowledge import project_setup
from boss_engine.tools import build_discuss_read_registry


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("kg-project-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def enforced():
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    yield
    settings.authz_mode, settings.authz_router_modes = saved


def _slug() -> str:
    return f"kg-map-{uuid.uuid4().hex[:6]}"


def _setup(client, slug):
    got = client.get("/v1/knowledge/setup", params={"project_id": slug, "actor": "ceo"})
    assert got.status_code == 200, got.text
    return got.json()


def test_a_knowledge_graph_project_is_made_listed_and_set_up_in_steps(world, owner, client, engine):
    slug = _slug()
    with world.acting_as(world.ctx(owner)):
        made = client.post("/v1/projects", json={"name": "Fineract map", "slug": slug,
                                                 "project_type": "knowledge_graph"})
        assert made.status_code == 201, made.text
        assert made.json()["project_type"] == "knowledge_graph"
        listed = {p["id"]: p for p in client.get("/v1/projects").json()["items"]}
        assert listed[slug]["project_type"] == "knowledge_graph"

        # THE GRAPH IS ON FOR ITS AGENTS, pinned on the project (an org default cannot turn it off).
        with engine.connect() as db:
            stored = db.execute(text("SELECT value::text FROM project_settings WHERE project_id = :p "
                                     "AND key = 'knowledge_graph'"), {"p": slug}).scalar()
        assert stored == '"on"'

        # STEP 1 IS NEXT: no repository yet; the scan and review wait for it.
        state = _setup(client, slug)
        assert state["project_type"] == "knowledge_graph" and state["next"] == "import"
        assert [(s["key"], s["state"]) for s in state["steps"]] == [
            ("import", "current"), ("team", "waiting"), ("scan", "waiting"), ("review", "waiting")]
        assert state["steps"][0]["summary"] == "No repository yet"
        # The team's candidates are the project's REAL agents, the recommended ones first, each
        # with its job here and its Passport's essentials.
        cands = state["team"]["candidates"]
        assert cands[0]["role_key"] == "chief_architect" and cands[0]["job"] == "scans"
        assert cands[0]["display_name"] == "Chief Architect" and cands[0]["mission"]
        recommended = [c["role_key"] for c in cands if c["recommended"]]
        assert recommended[:3] == ["chief_architect", "software_engineer", "sre"]
        assert state["team"]["chosen"] is False

        # STEP 2: the team, STATED (exactly these agents), through the existing assignment route.
        team = client.post(f"/v1/projects/{slug}/agents", json={
            "app": "*", "action": "set", "role_keys": ["chief_architect", "sre", "software_engineer"],
            "actor": "ceo"})
        assert team.status_code == 200, team.text
        assert team.json()["team"] == ["chief_architect", "sre", "software_engineer"]
        bad = client.post(f"/v1/projects/{slug}/agents", json={
            "app": "*", "action": "set", "role_keys": ["chief_archtect"], "actor": "ceo"})
        assert bad.status_code == 422 and "chief_archtect" in bad.text
        state = _setup(client, slug)
        assert state["steps"][1]["state"] == "done" and state["team"]["chosen"] is True
        assert sorted(state["team"]["role_keys"]) == ["chief_architect", "software_engineer", "sre"]
        assert state["steps"][1]["summary"] == "3 agents chosen"
        # Setting it again REPLACES it: nobody is left on from before.
        client.post(f"/v1/projects/{slug}/agents", json={
            "app": "*", "action": "set", "role_keys": ["chief_architect"], "actor": "ceo"})
        assert _setup(client, slug)["team"]["role_keys"] == ["chief_architect"]

        refused = client.post("/v1/projects", json={"name": "Typo", "slug": _slug(),
                                                    "project_type": "graph"})
        assert refused.status_code == 422 and "knowledge_graph" in refused.text
        plain = client.post("/v1/projects", json={"name": "An app", "slug": _slug()})
        assert plain.status_code == 201 and plain.json()["project_type"] == "app"


def test_the_steps_follow_the_records_so_the_setup_resumes(world, engine, monkeypatch):
    """Indexing → indexed → a scan → rules proposed → reviewed: each state read from the records
    (repo_index, workflow runs, rule nodes), never from a flag the setup keeps."""
    repos: list[dict] = []
    monkeypatch.setattr(project_setup.repo_index, "status",
                        lambda c, p: {"project_id": p, "repositories": repos})
    with engine.connect() as db:
        repos[:] = [{"repo": "acme/core", "status": "running", "files": 900, "parsed": 300,
                     "graph": {"nodes": 0, "edges": 0}, "summary": "Indexing main (running)."}]
        state = project_setup.status(db, world.project_a)
        steps = {s["key"]: s["state"] for s in state["steps"]}
        # Indexing runs on its own, so the team step is the one to do meanwhile.
        assert steps == {"import": "working", "team": "current", "scan": "waiting",
                         "review": "waiting"}
        repos[:] = [{"repo": "acme/core", "status": "indexed", "files": 900, "parsed": 900,
                     "graph": {"nodes": 4200, "edges": 9000}, "summary": "4,200 nodes"}]
        state = project_setup.status(db, world.project_a)
        assert state["steps"][0] == {"key": "import", "state": "done", "done": True,
                                     "summary": "1 of 1 repositories indexed · 4,200 code nodes"}
        assert state["has_graph"] is True and state["code_nodes"] == 4200
        assert state["tools"]["next"].startswith("set_project_team")


def test_the_agent_walks_the_same_steps(world, owner, engine, enforced):
    """create_project(project_type=knowledge_graph) → knowledge_project_setup → set_project_team,
    from a conversation in another project: the same services as the console's buttons."""
    registry = build_discuss_read_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    me = world.ctx(owner)

    def call(tool, **args):
        return registry.invoke(tool, ctx, project_id=world.project_a, actor="test", args=args,
                               auth=me)

    slug = _slug()
    made = call("create_project", name="Ledger map", slug=slug, project_type="knowledge_graph")
    assert made["ok"] is True and made["project_type"] == "knowledge_graph", made
    assert made["href"] == f"/?project_id={slug}&area=knowledge&kg_tab=setup"
    assert any("set_project_team" in s for s in made["next_steps"])

    listed = {p["id"]: p for p in call("list_projects")["projects"]}
    assert listed[slug]["project_type"] == "knowledge_graph"
    described = call("describe_project", project=slug)
    assert described["project"]["project_type"] == "knowledge_graph"

    setup = call("knowledge_project_setup", project=slug)
    assert setup["ok"] is True and setup["next"] == "import", setup
    assert setup["tools"]["next"].startswith("list_org_repositories")
    assert "mission" not in setup["team"]["candidates"][0]

    team = call("set_project_team", project=slug, role_keys=["chief_architect", "sre"])
    assert team["ok"] is True and team["team"] == ["chief_architect", "sre"], team
    with engine.connect() as db:
        rows = db.execute(text("SELECT role_key, source FROM project_agents WHERE project_id = :p "
                               "AND app_key = '*' ORDER BY role_key"), {"p": slug}).all()
    assert [tuple(r) for r in rows] == [("chief_architect", "operator"), ("sre", "operator")]
    assert call("knowledge_project_setup", project=slug)["steps"][1]["state"] == "done"

    wrong = call("set_project_team", project=slug, role_keys=["nobody_here"])
    assert wrong["ok"] is False and "nobody_here" in wrong["error"]
