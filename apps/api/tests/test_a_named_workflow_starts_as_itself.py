"""A named workflow starts as itself, not as the default Build path (BOSSAPP-1357).

Ask BOSS staged a "New work" card for api_onboarding that showed Plan, You approve, Build, Review,
PR, Staging; pressing Start created a Minor Feature run titled "Start api_onboarding run" with
the inputs only in prose. When the person asks to run a named workflow the card names THAT
workflow and its own steps, carries its declared inputs as values, and Start sends the workflow
key; if inputs are missing the card says which. The model's side is stood in for at the router.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("cards-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-cards-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
        db.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path) VALUES "
            "(:a, :p, 'file', :ka, 'sla.mjs', 'server/sla.mjs')"),
            {"a": f"kgn_{uuid.uuid4().hex[:12]}", "p": slug, "ka": f"file:{uuid.uuid4().hex[:6]}"})
    return {"id": slug, **story}


def _stub(monkeypatch, reply: str, offers=(), decision=None):
    def route_turn(*_args, **_kwargs):
        return SimpleNamespace(reply=reply, tool_calls=[], actions=[],
                               proposed_actions=list(offers), pending_confirmation=None,
                               next_steps=[], decision=decision or {})

    monkeypatch.setattr(main.orchestration_agent.router, "route_turn", route_turn)


def _turn(engine, pid: str, message: str, thread_id=None) -> dict:
    with engine.begin() as c:
        return main.orchestration_agent.chat(c, project_id=pid, actor="ceo", message=message,
                                             thread_id=thread_id)


def _run_number(engine, pid: str) -> int:
    from boss_engine.services.project_experience import work

    with engine.begin() as c:
        found = [w for w in work.everything(c, pid) if w.get("run_number")]
    return int(found[0]["run_number"])



def _publish_api_onboarding(engine, pid):
    from boss_engine.tools.builtin import build_default_registry

    with engine.begin() as c:
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                       "VALUES (:i, :p, 'gw_editor', 'gw_editor') ON CONFLICT DO NOTHING"),
                  {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid})
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                       "VALUES (:i, :p, 'gw_editor', 'drive.list_videos', 'builtin')"),
                  {"i": f"rs_{uuid.uuid4().hex[:8]}", "p": pid})

    class _Ctx:
        def begin(self):
            return engine.begin()

    tool = {s.name: s for s in build_default_registry().specs()}["draft_workflow"]
    out = tool.handler(_Ctx(), project_id=pid, actor="ceo", args={
        "workflow_key": "api_onboarding", "name": "API onboarding",
        "inputs": ["tenant_name", "tenant_yaml_path"],
        "steps": [
            {"id": "validate", "agent": True, "instructions": "Lint the tenant YAML."},
            {"id": "release_approval", "capability": "release_approval"},
            {"id": "smoke_check", "capability": "smoke_test",
             "input": {"url": "https://gw.example/health"}}]})
    assert out.get("status") == "published", out


def test_naming_a_workflow_stages_that_workflow_with_its_steps_and_structured_inputs(
        engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    offer = {"id": "act_offer_onb", "tool": "start_workflow", "label": "Start api_onboarding",
             "args": {"workflow_key": "api_onboarding", "directive": "Onboard Lumen Pay",
                      "inputs": {"tenant_name": "Lumen Pay",
                                 "tenant_yaml_path": "tenants/lumen-pay.yaml"}}}
    _stub(monkeypatch, "Offered below; nothing has started.", [offer],
          decision={"action": "new_code_change"})
    out = _turn(engine, pid, "start api_onboarding with Lumen Pay bad config")

    card = out["proposal"]
    assert card["workflow"] == "API onboarding" and card["workflow_key"] == "api_onboarding"
    assert card["workflow_version"] == 1
    # Its OWN steps, never the generic Build path.
    labels = [s["label"] for s in card["steps"]]
    assert labels[0] == "Validate", labels
    assert not {"Plan", "Build", "PR"} & set(labels), labels
    assert len(labels) == 3
    body = card["start"]["body"]
    assert body["workflow_key"] == "api_onboarding"
    assert body["inputs"] == {"tenant_name": "Lumen Pay",
                              "tenant_yaml_path": "tenants/lumen-pay.yaml"}
    assert "missing_inputs" not in card


def test_a_named_workflow_missing_inputs_says_which(engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    offer = {"id": "act_offer_onb2", "tool": "start_workflow", "label": "Start api_onboarding",
             "args": {"workflow_key": "api_onboarding", "directive": "Onboard Lumen Pay",
                      "inputs": {"tenant_name": "Lumen Pay"}}}
    _stub(monkeypatch, "Offered below.", [offer], decision={"action": "new_code_change"})
    card = _turn(engine, pid, "start api_onboarding with Lumen Pay bad config")["proposal"]
    assert card["workflow_key"] == "api_onboarding"
    assert card["missing_inputs"]["names"] == ["tenant_yaml_path"]
    assert "tenant_yaml_path" in card["missing_inputs"]["message"]


def test_prose_alone_never_picks_a_workflow(engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    _stub(monkeypatch, "Offered.", decision={"action": "new_code_change"})
    card = _turn(engine, pid, "Add an API onboarding tour to the app")["proposal"]
    assert "workflow_key" not in card and card["steps"][0]["label"] == "Plan"


def test_a_catalogue_workflow_offer_keeps_the_estimate_and_kind_card(engine, helpdesk,
                                                                    monkeypatch):
    pid = helpdesk["id"]
    offer = {"id": "act_offer_cat", "tool": "start_workflow", "label": "Fix it",
             "args": {"workflow_key": "minor_feature", "directive": "Fix the SLA bug"}}
    _stub(monkeypatch, "Offered.", [offer], decision={"action": "new_code_change"})
    card = _turn(engine, pid, "fix the SLA bug")["proposal"]
    assert "workflow_key" not in card and card["steps"][0]["label"] == "Plan"


def test_a_failing_named_card_falls_back_to_the_generic_card(engine, helpdesk, monkeypatch):
    from boss_engine.services.project_experience import new_work

    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    real = new_work.plan

    def boom(c, p, kind="feature", workflow_key=None, **kw):
        if workflow_key:
            raise RuntimeError("plan exploded")
        return real(c, p, kind, workflow_key, **kw)

    monkeypatch.setattr(new_work, "plan", boom)
    offer = {"id": "act_offer_f", "tool": "start_workflow", "label": "Start it",
             "args": {"workflow_key": "api_onboarding", "directive": "Onboard"}}
    _stub(monkeypatch, "Offered.", [offer], decision={"action": "new_code_change"})
    card = _turn(engine, pid, "start api_onboarding")["proposal"]
    assert card is not None and "workflow_key" not in card


def test_forged_inputs_the_workflow_does_not_declare_are_refused(
        world, client, engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    started = []
    monkeypatch.setattr(main, "start_delivery_workflow",
                        lambda body, ticket=None: started.append(body) or {"run": {"id": "wfr_x"}})
    with world.acting_as(world.ctx(world.owner)):
        res = client.post(f"/v1/projects/{pid}/work", json={
            "kind": "feature", "text": "Onboard", "workflow_key": "api_onboarding",
            "inputs": {"tenant_name": "Lumen Pay", "ticket": {"key": "FORGED-1"},
                       "repository": "evil/repo"}})
    assert res.status_code == 422, res.text
    assert "ticket" in res.text and "repository" in res.text
    assert not started


def test_start_sends_the_named_workflow_and_its_inputs_to_the_run(
        world, client, engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _publish_api_onboarding(engine, pid)
    seen = {}

    def fake_start(body, ticket=None):
        seen["body"] = body
        return {"run": {"id": "wfr_fake"}}

    monkeypatch.setattr(main, "start_delivery_workflow", fake_start)
    with world.acting_as(world.ctx(world.owner)):
        res = client.post(f"/v1/projects/{pid}/work", json={
            "kind": "feature", "text": "Onboard Lumen Pay", "title": "Onboard Lumen Pay",
            "workflow_key": "api_onboarding",
            "inputs": {"tenant_name": "Lumen Pay", "tenant_yaml_path": "t/lumen.yaml"}})
    assert res.status_code == 200, res.text
    body = seen["body"]
    assert body.workflow_key == "api_onboarding"
    assert body.input["tenant_name"] == "Lumen Pay"
    assert body.input["tenant_yaml_path"] == "t/lumen.yaml"
    assert body.input["title"] == "Onboard Lumen Pay"
