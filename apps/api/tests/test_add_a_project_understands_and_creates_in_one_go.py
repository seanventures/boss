"""Add a project v2 (new-project-v2 wireframes): Helm's understanding through the one agent
pipeline, the preview the six boxes are picked from, and Create and start as ONE transaction.

The model is a stub adapter (any engine answers the same structured decision); no GitHub, no ssh.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.routers import add_project as route
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services.project_experience import add_project

WORDS = ("A helpdesk for our support team.\nCustomers email support@ and it becomes a ticket. "
         "AI drafts the reply from our past answers; staff sign in with Google, edit and approve "
         "before it sends.\nNo chat or phone for now.")
ANSWER = {
    "title": "HelpDesk AI: email support with approved AI replies",
    "problem": "Replies are slow and uneven; staff retype answers they already wrote.",
    "who_uses": "Support staff (Google sign-in) · customers by email only",
    "must_haves": ["An email to support@ becomes a ticket", "AI drafts a reply from past answers",
                   "Staff edit and approve; nothing sends without approval"],
    "out_of_scope": ["Live chat", "phone", "a customer portal"],
    "done_when": "An email reaches the inbox as a ticket within a minute, with a draft.",
    "open_questions": [{"question": "Which mailbox does support@ live in?",
                        "options": ["Google Workspace", "Microsoft 365", "Other"]}],
    "added": ["nothing sends without approval", "a customer portal", "not in any field",
              "Live chat"],
    "stack": {"key": "react-vite+fastapi+postgres", "because": "AI drafts"},
    "connectors": [{"key": "login-oauth", "because": "sign in with Google"},
                   {"key": "no-such-connector", "because": "made up"},
                   {"key": "transactional-email", "because": "email support@"}],
    "workflow": {"key": "standard_product_delivery", "because": "a new app"},
}


class StubModel:
    """A routed chat model: answers the structured decision with `answer`, or raises."""

    def __init__(self, answer=None, *, fail=False):
        self.answer, self.fail, self.prompts = answer, fail, []

    def create_text_response(self, prompt, system_prompt=""):
        self.prompts.append((system_prompt, prompt))
        if self.fail:
            raise RuntimeError("no model is connected")
        return json.dumps({"action": "brief", "confidence": 0.9, "reason": "read the words",
                           "arguments": self.answer})


CATALOGUE = [{"key": "login-oauth", "name": "Login", "summary": "sign in", "stacks": ["python"]},
             {"key": "transactional-email", "name": "Email", "summary": "send", "stacks": ["python"]}]
STACKS = [{"id": "react-vite+fastapi+postgres", "label": "React + FastAPI", "ready": True}]
FLOWS = [{"key": "standard_product_delivery", "name": "Standard", "steps": 15, "approvals": 4}]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("np-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("np-viewer")
    w.bind(w.viewer, "viewer", project=w.project_a)
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


# ── Helm's understanding ────────────────────────────────────────────────────────────────────

def test_helm_rewrites_the_words_through_the_one_pipeline_with_its_additions_marked(engine, world):
    model = StubModel(ANSWER)
    with engine.begin() as db:
        out = add_project.understand(db, get_settings(), world.project_a, words=WORDS,
                                     catalogue=CATALOGUE, stacks=STACKS, workflows=FLOWS,
                                     adapter=model)
    assert out["status"] == "ok", out
    brief = out["brief"]
    assert brief["title"].startswith("HelpDesk AI")
    assert brief["must_haves"][0] == "An email to support@ becomes a ticket"
    assert brief["open_questions"] == [{"question": "Which mailbox does support@ live in?",
                                        "options": ["Google Workspace", "Microsoft 365", "Other"],
                                        "answer": None}]
    # Marked only where it is really in the brief and not the person's own words.
    assert brief["added"] == ["nothing sends without approval", "a customer portal", "Live chat"]
    # Picks are offered keys only, each with the words that led to it.
    picks = out["picks"]
    assert [c["key"] for c in picks["connectors"]] == ["login-oauth", "transactional-email"]
    assert picks["connectors"][0]["because"] == "sign in with Google"
    assert picks["stack"]["key"] == "react-vite+fastapi+postgres"
    assert picks["workflow"]["key"] == "standard_product_delivery"
    system, prompt = model.prompts[0]
    assert "Helm" in system and "support@" in prompt and "login-oauth" in prompt


def test_a_rewrite_keeps_the_lines_the_person_edited(engine, world):
    model = StubModel(ANSWER)
    with engine.begin() as db:
        out = add_project.understand(
            db, get_settings(), world.project_a, words=WORDS, hint="shorter",
            pinned={"done_when": "A draft appears in under 10 s"}, previous=ANSWER,
            catalogue=CATALOGUE, stacks=STACKS, workflows=FLOWS, adapter=model)
    assert out["brief"]["done_when"] == "A draft appears in under 10 s"
    assert out["brief"]["pinned"] == ["done_when"]
    assert "shorter" in model.prompts[0][1]


def test_no_model_keeps_the_words_and_lets_the_person_continue(engine, world):
    with engine.begin() as db:
        out = add_project.understand(db, get_settings(), world.project_a, words=WORDS,
                                     catalogue=CATALOGUE, stacks=STACKS, workflows=FLOWS,
                                     adapter=StubModel(fail=True))
    assert out["status"] == "no_model"
    assert out["brief"]["raw"] is True and out["brief"]["added"] == []
    assert out["brief"]["must_haves"][0].startswith("Customers email support@")
    assert "continue" in out["message"]


def test_a_turn_past_its_deadline_is_a_timeout_through_the_one_understanding_service(engine, world):
    from boss_engine.services.call_deadline import TurnDeadlineExceeded

    class Slow(StubModel):
        def create_text_response(self, prompt, system_prompt=""):
            raise TurnDeadlineExceeded("the turn's deadline passed")

    with engine.begin() as db:
        out = add_project.understand(db, get_settings(), world.project_a, words=WORDS,
                                     catalogue=CATALOGUE, stacks=STACKS, workflows=FLOWS,
                                     adapter=Slow())
    assert out["status"] == "timeout" and out["brief"]["raw"] is True
    assert "continue" in out["message"]


def test_an_answer_without_must_haves_is_refused_not_shown(engine, world):
    with engine.begin() as db:
        out = add_project.understand(db, get_settings(), world.project_a, words=WORDS,
                                     catalogue=CATALOGUE, stacks=STACKS, workflows=FLOWS,
                                     adapter=StubModel({"title": "X", "must_haves": [],
                                                        "done_when": "y"}))
    assert out["status"] == "no_model" and out["brief"]["raw"] is True


def test_the_brief_becomes_the_prds_directive_with_answers_and_open_questions():
    brief = add_project.shape_brief(ANSWER, words=WORDS)
    brief["open_questions"][0]["answer"] = "Google Workspace"
    brief["open_questions"].append({"question": "Daily summary by email or Slack?",
                                    "options": [], "answer": None})
    said = add_project.render_brief(brief, WORDS)
    assert said.splitlines()[0] == ANSWER["title"]
    assert "Must-haves:\n- An email to support@ becomes a ticket" in said
    assert "Decided:\n- Which mailbox does support@ live in? Google Workspace" in said
    assert "Open questions for the PRD:\n- Daily summary by email or Slack?" in said
    assert said.endswith(WORDS)


def test_the_understand_route_answers_and_a_slow_model_is_a_timeout_not_an_error(
        client, world, monkeypatch):
    monkeypatch.setattr(add_project, "understand", lambda *a, **k: {
        "status": "ok", "brief": add_project.shape_brief(ANSWER, words=WORDS),
        "picks": {"stack": None, "connectors": [], "workflow": None}})
    with _owner(world):
        r = client.post(f"/v1/projects/{world.project_a}/add-project/understand",
                        json={"words": WORDS})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["brief"]["title"].startswith("HelpDesk")

    monkeypatch.setattr(add_project, "WAIT_S", 0.2)
    monkeypatch.setattr(route, "_SLACK_S", 0.0)
    monkeypatch.setattr(add_project, "understand", lambda *a, **k: time.sleep(1) or {})
    with _owner(world):
        r = client.post(f"/v1/projects/{world.project_a}/add-project/understand",
                        json={"words": WORDS})
    assert r.status_code == 200 and r.json()["status"] == "timeout"
    assert r.json()["brief"]["raw"] is True


def test_reading_the_words_needs_no_step_up_and_create_still_does(client, world, monkeypatch):
    """boss-1, 2026-11-08: "enforce denied … step_up_required project:create" on Enter in the
    idea box. Reading and rewriting create nothing: understand, connectors and preview pass with
    a second factor long lapsed (assistant:chat / project:read, no step-up); Create and start
    still asks for it (project:create); a viewer still may not spend a model turn."""
    monkeypatch.setattr(add_project, "understand", lambda *a, **k: {
        "status": "ok", "brief": add_project.shape_brief(ANSWER, words=WORDS),
        "picks": {"stack": None, "connectors": [], "workflow": None}})
    lapsed = world.ctx(world.owner, mfa=False)
    with world.acting_as(lapsed):
        r = client.post(f"/v1/projects/{world.project_a}/add-project/understand",
                        json={"words": WORDS})
        assert r.status_code == 200, r.text
        assert r.json()["brief"]["title"].startswith("HelpDesk")
        r = client.post(f"/v1/projects/{world.project_a}/add-project/connectors",
                        json={"brief": ANSWER, "words": WORDS})
        assert r.status_code == 200, r.text
        r = client.get(f"/v1/projects/{world.project_a}/add-project/preview",
                       params={"mode": "idea"})
        assert r.status_code == 200, r.text
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "idea",
            "name": f"Lapsed {uuid.uuid4().hex[:6]}", "words": WORDS})
        assert r.status_code == 401, r.text
        assert r.json()["error"] == "step_up_required"
    with world.acting_as(world.ctx(world.viewer, mfa=True)):
        r = client.post(f"/v1/projects/{world.project_a}/add-project/understand",
                        json={"words": WORDS})
    assert r.status_code in (403, 404), r.text


# ── what BOSS will do ───────────────────────────────────────────────────────────────────────

def test_the_preview_reads_real_workflows_hosts_and_readiness(client, world, engine, monkeypatch):
    from boss_engine.services import deployment_fleet as fleet

    monkeypatch.setattr(fleet, "_first_free_port", lambda c, a, *, start=3000, end=None: 8005)
    hid = f"dh_{uuid.uuid4().hex[:10]}"
    with _owner(world):   # a seeded project: its workflows are the catalogue's, installed
        home = client.post("/v1/projects", json={"name": f"Home {hid}"}).json()["id"]
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, "
            "port_min, port_max, created_by) VALUES (:i, :p, 'boss-1', '203.0.113.9', 'deploy', "
            "'verified', 8000, 8099, 'test')"), {"i": hid, "p": home})
    with _owner(world):
        r = client.get(f"/v1/projects/{home}/add-project/preview",
                       params={"mode": "idea", "app_name": "HelpDesk AI"})
    assert r.status_code == 200, r.text
    view = r.json()
    flows = {w["key"]: w for w in view["workflows"]}
    assert flows, "the installed workflows"
    some = next(iter(flows.values()))
    assert some["steps"] == len(some["step_list"]) and some["steps"] > 0
    assert some["approvals"] == sum(1 for s in some["step_list"] if s["you"])
    assert view["default_workflow"] in flows
    assert [d["key"] for d in view["readiness"]] == ["model", "repository", "budget",
                                                     "deployment_target"]
    host = next(h for h in view["staging"] if h["host_id"] == hid)
    assert host["port"] == 8005 and host["host"] == "boss-1"
    assert view["captain"]["candidates"]


@pytest.mark.parametrize("prd_auto", [True, False])
def test_a_step_is_you_approve_only_where_the_run_stops_for_a_person(client, world, monkeypatch,
                                                                      prd_auto):
    """The box count, the strip, its legend and the popover read ONE `you` per step, and it is
    what the run does (stops_for_a_person under this install's PRD setting). A step labelled
    "(automatic)" is never a person's approval, and a step a person signs never says automatic.
    The legend's words are the few-word `short` ("PRD", "deploy"), not the step's full label."""
    from boss_engine.services import workflow_capabilities as caps

    monkeypatch.setattr(caps, "prd_auto_approved", lambda settings=None: prd_auto)
    with _owner(world):
        home = client.post("/v1/projects", json={"name": f"Home {uuid.uuid4().hex[:8]}"}).json()["id"]
        view = client.get(f"/v1/projects/{home}/add-project/preview", params={"mode": "idea"}).json()
    prd_steps = 0
    for w in view["workflows"]:
        assert w["approvals"] == sum(1 for s in w["step_list"] if s["you"])
        assert w["approve_words"] == list(dict.fromkeys(s["short"] for s in w["step_list"] if s["you"]))[:6]
        for s in w["step_list"]:
            assert s["short"] and len(s["short"].split()) <= 2 and "(" not in s["short"], s
            if "(automatic)" in s["label"]:
                assert not s["you"], f"{w['key']}: {s['label']} is automatic, not a person's"
            if s["label"].startswith("PRD approval"):
                prd_steps += 1
                assert s["you"] is (not prd_auto) and s["short"] == "PRD"
                assert ("(automatic)" in s["label"]) is prd_auto
    assert prd_steps, "the catalogue's PRD approval step was read"


# ── Create and start: one transaction ───────────────────────────────────────────────────────

def _exists(engine, name: str) -> bool:
    with engine.connect() as db:
        return bool(db.execute(text("SELECT 1 FROM projects WHERE name = :n"), {"n": name}).first())


def test_create_and_start_an_idea_is_all_or_nothing(client, world, engine):
    name = f"HelpDesk {uuid.uuid4().hex[:6]}"
    body = {"context_project_id": world.project_a, "kind": "idea", "name": name, "words": WORDS,
            "brief": {**ANSWER, "open_questions": [{**ANSWER["open_questions"][0],
                                                    "answer": "Google Workspace"}]},
            "inputs": {"notify_channel": "email:lead@example.com"}, "notify_when": ["needs_you"],
            "audience": "A support agent on a laptop", "done_when": ["A draft in under 10 s"]}
    with _owner(world):
        first = client.post("/v1/add-project", json=body)
    if first.status_code != 200:
        # Refused for what this install lacks: nothing was left behind, and the step is named.
        said = first.json()
        assert said["created"] is False and said["step"] == "start", said
        assert not _exists(engine, name)
        missing = [m["key"] for m in (said.get("readiness") or {}).get("missing") or []]
        assert missing, said
        body = {**body, "start_anyway": missing}
        with _owner(world):
            first = client.post("/v1/add-project", json=body)
    if first.status_code == 409 and first.json().get("error") == "consent_required":
        # Only the start could ask this: the project is kept, and the retry names it — never a
        # second project. The console's consent sheet answers, then the same call goes again.
        said = first.json()
        assert said["created"] is True and said["retry"]["project_id"] == said["project_id"]
        grant = {"agents": [a["id"] for a in said["agents"]], "permissions": said["permissions"],
                 "projects": said["projects"], "kind": "standing", "days": 30,
                 "purpose": said["task"]}
        with _owner(world):
            assert client.post("/v1/identity/me/delegations", json=grant).status_code in (200, 201)
            first = client.post("/v1/add-project", json={**body, **said["retry"]})
        with engine.connect() as db:
            assert db.execute(text("SELECT count(*) FROM projects WHERE name = :n"),
                              {"n": name}).scalar() == 1
    assert first.status_code == 200, first.text
    made = first.json()
    pid = made["project_id"]
    assert made["created"] and made["started"].get("run_id")
    assert made["href"].endswith("tab=overview") and pid in made["href"]
    with engine.connect() as db:
        seed = add_project.seed_of(db, pid)
        run = db.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                         {"r": made["started"]["run_id"]}).scalar()
    assert seed["kind"] == "idea" and seed["brief"]["title"].startswith("HelpDesk AI")
    assert seed["picks"]["notify"] == {"channel": "email:lead@example.com", "when": ["needs_you"]}
    run = run if isinstance(run, dict) else json.loads(run)
    assert "Decided:\n- Which mailbox does support@ live in? Google Workspace" in run["directive"]
    assert "Who it is for: A support agent on a laptop" in run["directive"]
    assert run["acceptance_criteria"][0] == ANSWER["done_when"]

    # Where it lands (c-after-create): the seed, the first run's stations and whose move it is.
    with _owner(world):
        view = client.get(f"/v1/projects/{pid}/add-project/started")
    assert view.status_code == 200, view.text
    view = view.json()
    assert view["seed"]["brief"]["title"].startswith("HelpDesk AI")
    assert view["run"]["id"] == made["started"]["run_id"]
    assert view["nodes"] and {n["state"] for n in view["nodes"]} <= {"done", "now", "you", "todo", "failed"}
    assert view["workflow"]["steps"] > 0


def test_a_name_in_use_refuses_before_anything_is_written(client, world, engine):
    name = f"Taken {uuid.uuid4().hex[:6]}"
    with _owner(world):
        assert client.post("/v1/projects", json={"name": name}).status_code == 201
        r = client.post("/v1/add-project", json={"context_project_id": world.project_a,
                                                 "kind": "idea", "name": name, "words": WORDS})
    assert r.status_code == 409 and r.json()["step"] == "project"
    assert "exists" in r.json()["message"]


def test_a_repository_refusal_rolls_back_the_project(client, world, engine):
    name = f"Repo {uuid.uuid4().hex[:6]}"
    rid = f"ghr_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_repos (id, org_id, full_name, allowed, checks) "
                        "VALUES (:i, :o, :f, false, '{}'::jsonb)"),
                   {"i": rid, "o": world.org, "f": f"acme/{rid}"})
    with _owner(world):
        r = client.post("/v1/add-project", json={"context_project_id": world.project_a,
                                                 "kind": "repo", "name": name, "repo_ids": [rid]})
    assert r.status_code == 409, r.text
    assert r.json()["step"] == "repository" and "Repo and models" in r.json()["message"]
    assert not _exists(engine, name)


def test_a_repository_project_starts_onboarding_with_the_note(client, world, engine, monkeypatch):
    from boss_engine.multirepo import project_repos

    rid = f"ghr_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_repos (id, org_id, full_name, allowed, checks) "
                        "VALUES (:i, :o, :f, true, '{}'::jsonb)"),
                   {"i": rid, "o": world.org, "f": f"acme/{rid}"})
    bound = []
    monkeypatch.setattr(project_repos, "put", lambda db, s, pid, ref, **k: bound.append((pid, ref)))
    name = f"Existing {uuid.uuid4().hex[:6]}"
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "repo", "name": name,
            "repo_ids": [rid], "note": "start with the SLA bugs", "map_rules": True})
    assert r.status_code == 200, r.text
    made = r.json()
    assert bound == [(made["project_id"], rid)]
    assert made["started"]["onboarding"]["run_id"] and made["href"].endswith("tab=onboarding")
    with engine.connect() as db:
        kind = db.execute(text("SELECT project_type FROM projects WHERE id = :p"),
                          {"p": made["project_id"]}).scalar()
        from boss_engine.services.project_experience import onboarding

        view = onboarding.view(db, made["project_id"])
    assert kind == "knowledge_graph"
    assert view["seed"]["note"] == "start with the SLA bugs"


def test_a_viewer_cannot_create_and_bad_input_is_a_sentence(client, world):
    with world.acting_as(world.ctx(world.viewer)):
        r = client.post("/v1/add-project", json={"context_project_id": world.project_a,
                                                 "kind": "idea", "name": "x", "words": WORDS})
    assert r.status_code == 403
    with _owner(world):
        r = client.post("/v1/add-project", json={"context_project_id": world.project_a,
                                                 "kind": "repo", "name": "x"})
    assert r.status_code == 422 and r.json()["message"] == "Pick a repository."


def test_what_helm_found_in_a_repository(client, world, engine, monkeypatch):
    """The repo card: what it is, its main parts with file counts, health, what onboarding does."""
    from boss_engine.services import start_project

    paths = ["api/tickets/models.py", "api/tickets/views.py", "api/replies/gpt.py", "web/src/inbox/List.tsx",
             "web/src/inbox/Item.tsx", "web/src/inbox/Pane.tsx", "tests/test_sla.py", "tests/test_tickets.py",
             "pyproject.toml", "package.json", "README.md", "node_modules/x/index.js"]
    monkeypatch.setattr(start_project, "github_paths", lambda db, org, full, branch: list(paths))
    rid = f"ghr_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_repos (id, org_id, full_name, language, description, allowed, "
                        "checks) VALUES (:i, :o, :f, 'Python', 'Email in, AI-drafted replies out', true, "
                        "'{}'::jsonb)"), {"i": rid, "o": world.org, "f": f"acme/{rid}"})
    with world.acting_as(world.ctx(world.viewer)):
        r = client.get(f"/v1/projects/{world.project_a}/add-project/repo/{rid}")
    assert r.status_code == 200, r.text
    found = r.json()
    assert found["what_it_is"] == "Email in, AI-drafted replies out"
    assert found["parts"][:2] == [{"path": "web/src", "files": 3}, {"path": "api/tickets", "files": 2}]
    assert "node_modules" not in {p["path"] for p in found["parts"]}
    assert found["health"][0] == {"label": "2 test files", "tone": "green"}
    assert found["files_total"] == len(paths) and len(found["onboarding_will"]) == 5
    with world.acting_as(world.ctx(world.viewer)):
        assert client.get(f"/v1/projects/{world.project_a}/add-project/repo/ghr_nope").status_code == 404


def test_connectors_without_a_stack_refuse_before_anything_is_kept(client, world, engine):
    name = f"Cx {uuid.uuid4().hex[:6]}"
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "idea", "name": name, "words": WORDS,
            "connectors": ["login-oauth"], "accept_egress": ["accounts.google.com"]})
    assert r.status_code == 422, r.text
    assert r.json()["step"] == "connectors" and r.json()["message"] == "Pick a stack for the connectors."
    assert not _exists(engine, name)


def test_start_anyway_on_the_kept_project_still_may_apply_connectors(world):
    """Walk 2026-11-08: a readiness refusal kept the project; Start anyway resent the create with
    retry.project_id, and its connectors step answered "You may not apply connectors
    (template:read)": template:read is held org-wide, and was checked AT the project. Every
    step's permission is checked where it is held."""
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(identity=world.ctx(world.owner, mfa=True),
                                                identity_mode="enforce"))
    route._need(req, "template:read", "apply_connectors", world.project_a)   # no refusal
    route._need(req, "workflow_run:start", "start_the_plan", world.project_a)
    with pytest.raises(route.Refused):
        route._need(SimpleNamespace(state=SimpleNamespace(identity=world.ctx(world.viewer, mfa=True),
                                                          identity_mode="enforce")),
                    "workflow_run:start", "start_the_plan", world.project_a)


def test_talks_to_in_words_goes_into_the_directive_not_app_links():
    """More detail › Talks to "Xero API" was sent as an app link and refused the whole create
    ("an alias is A-Z, 0-9 and _…"). Words are what the app calls; only an app of the project,
    with its key and an alias, is a link."""
    body = route.CreateAndStartBody(context_project_id="p", name="Receipts", words=WORDS,
                                    talks_to=[{"name": "Xero API"},
                                              {"provider_app_key": "billing", "alias": "BILLING"}])
    run = route._run_input(body, {}, None)
    assert "It talks to: Xero API." in run["directive"]
    assert run["app_links"] == [{"provider_app_key": "billing", "alias": "BILLING"}]
    plain = route._run_input(route.CreateAndStartBody(context_project_id="p", name="R", words=WORDS,
                                                      talks_to=[{"name": "Stripe"}]), {}, None)
    assert "app_links" not in plain
