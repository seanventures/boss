"""The New work modal (docs/product/wireframes/project-experience/new-project-v2/d-new-work.html).

* Helm's understanding: POST …/work/understand is ONE turn through the one agent pipeline
  (AgentService.decide, a structured decision): the brief — title, what changes, area from the
  code graph, done when, risk, Helm's additions — updated from the whole conversation, Helm's
  reply and at most one question. A file the code graph does not hold is refused and asked
  again. Helm asks at most three questions on its own; past that the question goes on the card.
  No model: `no_model`, and Start uses the words.
* Picked for you: GET …/work/plan?detail=1 carries the workflow's real steps (approvals marked),
  each option's steps, estimate and old size, and the tracker / targets / who-to-tell defaults.
* Start: the confirmed brief is the run's directive (the spec), its Done when the build's
  acceptance criteria (the PR checklist), with priority and who to tell.
* Add to Next: POST …/plan puts the work on Roadmap › Next by priority; nothing starts, and its
  later Start sends what it was given.

Real Postgres; a scripted model (no paid call), no GitHub.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("nwm-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("nwm-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine, monkeypatch):
    from apps.api.tests import project_experience_seed as px_seed

    monkeypatch.setattr(px_seed, "seed_alert_history", lambda *a, **k: {})
    slug = f"nwm-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        seed(db, slug)
        db.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, language, default_branch, allowed, "
            "checks, checked_at) VALUES (:i, :o, :f, 'TypeScript', 'main', true, "
            "CAST(:c AS JSONB), now()) ON CONFLICT DO NOTHING"),
            {"i": f"ghr_{uuid.uuid4().hex[:8]}", "f": REPO, "o": world.org,
             "c": '{"read": {"ok": true}, "write": {"ok": true}, "pull_requests": {"ok": true}}'})
        # The code graph: three files that carry the words, one that does not.
        for kind, name, path in (("file", "intake.py", "api/tickets/intake.py"),
                                 ("function", "create_ticket", "api/tickets/intake.py"),
                                 ("file", "models.py", "api/tags/models.py"),
                                 ("file", "TicketRow.tsx", "web/src/inbox/TicketRow.tsx"),
                                 ("file", "billing.py", "api/billing/billing.py"),
                                 ("table", "tickets", None)):
            db.execute(text(
                "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path) VALUES "
                "(:i, :p, :k, :n, :name, :path)"),
                {"i": f"kgn_{uuid.uuid4().hex[:12]}", "p": slug, "k": kind,
                 "n": f"{path or ''}:{name}", "name": name, "path": path})
    return slug


def _as(world, who):
    return world.acting_as(world.ctx(who))


class Scripted:
    """A model answering the decision contract from a script; it records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[tuple[str, str]] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.prompts.append((system_prompt or "", prompt))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return "r", json.dumps(answer), {"model": "scripted"}


def _brief(**over):
    args = {"title": "Auto-tag new tickets by product",
            "what_changes": "Each new ticket gets a product tag, with a confidence.",
            "area": [{"path": "api/tickets/intake.py", "why": "writes tickets"},
                     {"path": "migrations/0042_ticket_product.py", "why": "tickets.product",
                      "new": True}],
            "done_when": ["A new ticket has a product tag within 10 s",
                          "The inbox filters by product"],
            "risk": {"level": "medium", "why": "touches intake"},
            "added": ["with a confidence"], "open_questions": [],
            "ask": {"text": "Tag the 12,000 existing tickets too, or only new ones?",
                    "replies": ["Only new", "Re-tag all once", "Last 30 days"]},
            "reply": "There's a tags table, but nothing writes product tags yet.",
            "applied": [], "settings": {}}
    args.update(over)
    return {"action": "brief", "confidence": 0.9, "reason": "r", "question": "",
            "arguments": args, "evidence": []}


@pytest.fixture
def model(monkeypatch):
    """The project's routed model, scripted: the pipeline's own routing seam."""
    from boss_engine.services import agent_decision

    holder: dict = {}

    def routed(connection, settings, project_id, ref, text_in):
        return holder["model"], holder["model"]

    monkeypatch.setattr(agent_decision, "_routed", routed)
    monkeypatch.setattr(agent_decision, "_jev_ready", lambda settings: False)

    def use(*answers):
        holder["model"] = Scripted(*answers)
        return holder["model"]
    return use


WORDS = ("Let agents tag tickets by product.\nWhen a ticket comes in, pick the product from the "
         "email and the customer's plan; staff can change it in the inbox.")


def _understand(world, client, pid, body, who=None):
    with _as(world, who or world.owner):
        r = client.post(f"/v1/projects/{pid}/work/understand", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── Helm's understanding ────────────────────────────────────────────────────────────────────

def test_helm_reads_the_words_and_the_code_graph_into_a_brief(world, client, project, model):
    scripted = model(_brief())
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS, "draft": "d1"})
    assert got["status"] == "understood"
    b = got["brief"]
    assert b["title"] == "Auto-tag new tickets by product"
    assert [a["path"] for a in b["area"]] == ["api/tickets/intake.py",
                                              "migrations/0042_ticket_product.py"]
    assert [a["new"] for a in b["area"]] == [False, True]
    assert b["done_when"][0].startswith("A new ticket") and b["risk"]["level"] == "medium"
    assert b["added"] == ["with a confidence"]
    assert got["ask"]["replies"] == ["Only new", "Re-tag all once", "Last 30 days"]
    assert got["agent"]["role_key"] == "engineering_captain" and got["asked"] == 1
    # Helm was shown the files that carry the words — and not the unrelated one.
    system, prompt = scripted.prompts[0]
    assert "api/tickets/intake.py" in prompt and "web/src/inbox/TicketRow.tsx" in prompt
    assert "api/billing/billing.py" not in prompt
    assert "KEEP THEIR WORDS" in system and "code_graph.files" in system


def test_a_file_the_graph_does_not_hold_is_refused_and_asked_again(world, client, project,
                                                                   model):
    scripted = model(_brief(area=[{"path": "api/made_up.py", "why": "guess"}]),
                     _brief())
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS})
    assert got["status"] == "understood"
    assert got["brief"]["area"][0]["path"] == "api/tickets/intake.py"
    assert len(scripted.prompts) == 2 and "api/made_up.py" in scripted.prompts[1][1]


def test_each_answer_updates_the_brief_and_what_was_asked_for(world, client, project, model):
    model(_brief(done_when=["A new ticket has a product tag within 10 s",
                            "Re-tags the 12,000 existing tickets once, in the background"],
                 ask={}, reply="Added it as a background job. Set both below.",
                 applied=["Done when +1", "priority high"],
                 settings={"priority": "high", "notify": "#support-eng"}))
    chat = [{"from": "helm", "text": "Tag the 12,000 existing tickets too?", "ask": True},
            {"from": "you", "text": "Re-tag all once. High priority, and tell #support-eng"}]
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS, "chat": chat})
    assert got["brief"]["done_when"][-1].startswith("Re-tags the 12,000")
    assert got["applied"] == ["Done when +1", "priority high"]
    assert got["settings"] == {"priority": "high", "notify": "#support-eng"}
    assert got["ask"] is None


def test_past_three_of_its_own_questions_helm_puts_the_next_on_the_card(world, client, project,
                                                                        model):
    scripted = model(_brief(ask={"text": "Which inbox view?", "replies": ["All", "Mine"]}))
    chat = []
    for n in range(3):
        chat += [{"from": "helm", "text": f"Question {n}?", "ask": True},
                 {"from": "you", "text": f"Answer {n}"}]
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS, "chat": chat})
    assert got["ask"] is None
    assert "Which inbox view?" in got["brief"]["open_questions"]
    assert "do NOT ask" in scripted.prompts[0][0]
    # The person may keep talking: nothing about the count is refused or shown as a limit.
    assert got["status"] == "understood" and got["asked"] == 3


def test_with_no_model_start_uses_the_words(world, client, project, monkeypatch):
    from boss_engine.services import agent_decision

    def down(*_a, **_k):
        raise RuntimeError("no model is connected")

    monkeypatch.setattr(agent_decision, "_routed", down)
    monkeypatch.setattr(agent_decision, "_jev_ready", lambda settings: False)
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS})
    assert got["status"] == "no_model" and got["brief"] is None
    assert "your words as written" in got["reply"]


def test_a_viewer_who_may_not_chat_is_refused(world, client, project):
    with _as(world, world.viewer):
        r = client.post(f"/v1/projects/{project}/work/understand",
                        json={"kind": "feature", "text": WORDS})
    assert r.status_code == 403


# ── Picked for you ──────────────────────────────────────────────────────────────────────────

def test_the_plan_in_detail_has_real_steps_option_sizes_and_the_chip_defaults(world, client,
                                                                             project):
    with _as(world, world.owner):
        got = client.get(f"/v1/projects/{project}/work/plan",
                         params={"kind": "feature", "detail": "true"}).json()
    steps = got["steps"]
    assert steps and all(s["label"] and "id" in s for s in steps)
    assert not any(s["id"] in ("pass_2", "pass_2_joined") for s in steps)
    assert any(s["you"] for s in steps)  # the release waits on a person
    minor = next(o for o in got["options"] if o["key"] == "minor_feature")
    assert minor["steps"] > 3 and minor["was"] == "Small" and minor["approvals"]
    d = got["defaults"]
    assert d["tracker"]["where"] == REPO and d["priority"] == "normal"
    assert {r["event"] for r in d["tell"]["routes"]} >= {"needs_you", "pr_ready"}
    assert isinstance(d["targets"], list)
    # Without detail the plan is what the Work tab and the answer card read (no extra reads).
    with _as(world, world.owner):
        plain = client.get(f"/v1/projects/{project}/work/plan", params={"kind": "feature"}).json()
    assert "defaults" not in plain and "steps" not in plain["options"][0]


# ── Start and Add to Next ───────────────────────────────────────────────────────────────────

@pytest.fixture
def no_repo_guard(monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)


def _start(world, client, pid, body):
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{pid}/work", json=body)
        for _ in range(3):
            if r.status_code == 200:
                break
            got = r.json() if r.status_code == 409 else {}
            if got.get("error") == "consent_required":
                grant = {"agents": [a["id"] for a in got["agents"]],
                         "permissions": got["permissions"], "projects": got["projects"],
                         "kind": "standing", "days": 30, "purpose": got["task"]}
                if got.get("github"):
                    grant["github"] = {"repos": got["github"]["repos"],
                                       **got["github"]["permissions"]}
                client.post("/v1/identity/me/delegations", json=grant)
            else:
                detail = got.get("detail") if isinstance(got.get("detail"), dict) else got
                ready = detail.get("readiness") if isinstance(detail.get("readiness"),
                                                              dict) else detail
                body = {**body, "start_anyway": [m["key"] for m in
                                                 (ready or {}).get("missing") or []]}
            r = client.post(f"/v1/projects/{pid}/work", json=body)
    return r


BRIEF = {"title": "Auto-tag new tickets by product",
         "what_changes": "Each new ticket gets a product tag.",
         "area": [{"path": "api/tickets/intake.py", "why": "writes tickets", "new": False}],
         "done_when": ["A new ticket has a product tag within 10 s", "The inbox filters by product"],
         "risk": {"level": "medium", "why": "touches intake"}, "added": [], "open_questions": []}


def test_start_runs_the_confirmed_brief_as_the_spec_and_its_checklist(world, client, engine,
                                                                     project, no_repo_guard):
    r = _start(world, client, project, {"kind": "feature", "text": WORDS, "brief": BRIEF,
                                        "priority": "high", "notify_channel": "#support-eng"})
    assert r.status_code == 200, r.text
    with engine.connect() as db:
        run_input = db.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                               {"r": r.json()["run_id"]}).scalar()
    assert run_input["title"] == "Auto-tag new tickets by product"
    assert run_input["acceptance_criteria"] == BRIEF["done_when"]
    assert run_input["directive"].startswith("Auto-tag new tickets by product")
    assert "- api/tickets/intake.py" in run_input["directive"]
    assert run_input["directive"].endswith(WORDS)  # the person's own words are kept
    assert run_input["priority"] == "high" and run_input["notify_channel"] == "#support-eng"


def test_add_to_next_plans_it_by_priority_and_its_start_sends_it_again(world, client, engine,
                                                                       project, no_repo_guard):
    with _as(world, world.owner):
        before = client.get(f"/v1/projects/{project}/plan").json()
        r = client.post(f"/v1/projects/{project}/plan",
                        json={"kind": "feature", "text": WORDS, "brief": BRIEF,
                              "priority": "high", "workflow_key": "minor_feature"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["lane"] == "next" and out["position"] == 1  # high goes before normal work
    nxt = out["plan"]["next"]
    assert nxt[0]["id"] == out["item_id"] and len(nxt) == len(before["next"]) + 1
    with engine.connect() as db:
        runs = db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                          {"p": project}).scalar()
    # Nothing started; the item's Start carries what it was given.
    start = nxt[0]["start"]["body"]
    assert start["workflow_key"] == "minor_feature" and start["priority"] == "high"
    assert start["acceptance_criteria"] == BRIEF["done_when"]
    assert start["source_ref"] == f"rec:{out['item_id']}"
    r2 = _start(world, client, project, start)
    assert r2.status_code == 200, r2.text
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                          {"p": project}).scalar() == runs + 1
    with _as(world, world.owner):
        after = client.get(f"/v1/projects/{project}/plan").json()
    assert not any(c["id"] == out["item_id"] for c in after["next"])


def test_a_viewer_cannot_add_to_next(world, client, project):
    with _as(world, world.viewer):
        r = client.post(f"/v1/projects/{project}/plan", json={"kind": "feature", "text": WORDS})
    assert r.status_code == 403


def test_the_directive_is_the_words_when_there_is_no_brief():
    from boss_engine.services.project_experience.understanding import directive

    assert directive("Fix the SLA timer", None) == "Fix the SLA timer"
    text_out = directive("Fix it", {**BRIEF, "open_questions": ["Weekends too?"]})
    assert "Done when:\n- A new ticket" in text_out and "Open questions:\n- Weekends too?" in text_out


def test_a_question_already_asked_is_never_asked_again(world, client, project, model):
    model(_brief(ask={"text": "Only new tickets, or all?", "replies": ["Only new", "All"]}))
    chat = [{"from": "helm", "text": "Only new tickets, or all?", "ask": True},
            {"from": "you", "text": "High priority please"}]
    got = _understand(world, client, project, {"kind": "feature", "text": WORDS, "chat": chat})
    assert got["ask"] is None and "Only new tickets, or all?" in got["brief"]["open_questions"]
