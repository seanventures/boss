"""An answer carries a typed "New work" card and record chips (screen 09, turn_cards).

On THE ONE AGENT TURN, whatever surface asked and whichever engine answered:

* a turn that offered to start work (`create_code_task` / `start_workflow`) — or that BOSS's
  decision read as new work — returns `proposal` {kind: new_work, title, text, estimate, steps,
  start (THE start door), edit_href, state}; a question returns none;
* `citations` are the records the reply named, resolved against the project: "PR #3" → that
  work's drawer, "Run #N" → the run, `sla.mjs` → the file in the repository (only a path the code
  graph knows), "Staging" → the environment drawer, "demo logins" → Settings; a PR the project
  never had and a file nobody knows are left as text;
* both are stored with the message, so the reopened thread shows them; Start / Not now are
  recorded on the card (POST /v1/engine/agent/proposal) and a start is written into the
  conversation and spends the offer.

Real Postgres and the HelpDesk seed; the model's side of the turn is stood in for at the router.
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


def test_an_offer_to_build_becomes_a_new_work_card_with_the_start_door(engine, helpdesk,
                                                                      monkeypatch):
    pid = helpdesk["id"]
    offer = {"id": "act_offer_vip", "tool": "create_code_task", "label": "Build the VIP flag",
             "args": {"title": "VIP customer flag",
                      "instructions": "Add a VIP customer flag that jumps the support queue."}}
    _stub(monkeypatch, "I'll plan it as a minor feature; it touches the SLA code.", [offer])
    out = _turn(engine, pid, "Also add a VIP customer flag that jumps the queue.")

    card = out["proposal"]
    assert card["kind"] == "new_work" and card["state"] == "open"
    assert card["title"] == "VIP customer flag"
    assert card["text"].startswith("Add a VIP customer flag")
    assert card["offer_id"] == "act_offer_vip"
    # Start is THE start door, with the message as its source.
    assert card["start"]["href"] == f"/v1/projects/{pid}/work"
    assert card["start"]["method"] == "POST"
    assert card["start"]["body"]["kind"] == "feature"
    assert card["start"]["body"]["source_ref"].startswith("chat:amsg")
    # Edit opens the Work tab's composer with the words in it.
    assert "tab=work" in card["edit_href"] and "compose=1" in card["edit_href"]
    assert "compose_text=Add+a+VIP" in card["edit_href"]
    # The plan strip ends at the pull request; the estimate is the workflow's own (minutes
    # from its steps, dollars only from this project's past runs — never a constant).
    labels = [s["label"] for s in card["steps"]]
    assert labels[0] == "Plan" and "PR" in labels
    assert card["estimate"] and card["estimate"]["minutes"]
    assert card["estimate"]["basis"]

    # Stored with the message: the reopened thread carries the same card.
    with engine.begin() as c:
        stored = main.orchestration_agent.list_messages(c, out["thread_id"])
    answer = [m for m in stored if m["role"] == "assistant"][-1]
    assert answer["proposal"]["title"] == "VIP customer flag"


def test_new_work_decided_without_an_offer_still_gets_a_card_and_a_question_gets_none(
        engine, helpdesk, monkeypatch):
    pid = helpdesk["id"]
    _stub(monkeypatch, "That's a new feature — offered below.",
          decision={"action": "new_code_change"})
    out = _turn(engine, pid, "Add an export to CSV button on the ticket list")
    assert out["proposal"]["text"] == "Add an export to CSV button on the ticket list"
    assert out["proposal"]["offer_id"] is None

    _stub(monkeypatch, "Two things are left before the demo.", decision={"action": "answer"})
    asked = _turn(engine, pid, "What's left before I can demo this?")
    assert asked["proposal"] is None


def test_the_reply_names_records_and_each_becomes_a_chip_that_opens_it(engine, helpdesk,
                                                                       monkeypatch):
    pid = helpdesk["id"]
    run_no = _run_number(engine, pid)
    reply = (f"Merge **PR #3** first (tests green), then Run #{run_no} finishes. The weekend bug "
             "sits in `sla.mjs`; staging redeploys after the merge, and the demo logins are in "
             "Settings. PR #999 and nothing.py are not this project's.")
    _stub(monkeypatch, reply)
    out = _turn(engine, pid, "What's left before I can demo this?")
    chips = out["citations"]
    by_kind = {(c["kind"], c["label"]): c for c in chips}

    pr = by_kind[("pr", "PR #3")]
    assert "tab=work" in pr["href"] and "drawer=run" in pr["href"] and not pr["external"]
    run = by_kind[("run", f"Run #{run_no}")]
    assert "drawer=run" in run["href"]
    f = by_kind[("file", "sla.mjs")]
    assert f["href"] == f"https://github.com/{REPO}/blob/main/server/sla.mjs" and f["external"]
    env = by_kind[("env", "Staging")]
    assert "tab=deployments" in env["href"] and "drawer=env" in env["href"]
    logins = by_kind[("place", "Demo logins")]
    assert "tab=settings" in logins["href"] and "section%3Ademo_logins" in logins["href"]
    # In the order the reply names them; an unknown file is text, a PR #999 is the repo's link.
    kinds = [c["kind"] for c in chips]
    assert kinds.index("pr") < kinds.index("run") < kinds.index("file") < kinds.index("env")
    assert not any(c["label"] == "nothing.py" for c in chips)
    assert len(chips) <= 6


def test_start_and_not_now_are_recorded_on_the_card(world, client, engine, helpdesk,
                                                    monkeypatch):
    pid = helpdesk["id"]
    offer = {"id": "act_offer_csv", "tool": "create_code_task", "label": "Build CSV export",
             "args": {"title": "CSV export", "instructions": "Add a CSV export."}}
    _stub(monkeypatch, "Offered below.", [offer])
    out = _turn(engine, pid, "Add a CSV export")
    msg = [m for m in out["messages"] if m["role"] == "assistant"][-1]["id"]

    with world.acting_as(world.ctx(world.owner)):
        said = client.post("/v1/engine/agent/proposal", json={
            "project_id": pid, "thread_id": out["thread_id"], "message_id": msg,
            "outcome": "started", "run_id": "wfr_started1", "href": "?area=project&tab=work"})
    assert said.status_code == 200, said.text
    assert said.json()["proposal"]["state"] == "started"
    with engine.begin() as c:
        stored = main.orchestration_agent.list_messages(c, out["thread_id"])
    card = next(m for m in stored if m["id"] == msg)["proposal"]
    assert card["state"] == "started" and card["run_id"] == "wfr_started1"
    # The conversation knows it started (the next turn will not offer it again), and the offer
    # is spent.
    assert any("Start · CSV export" in (m["content"] or "") for m in stored)
    assert all(not any(o.get("id") == "act_offer_csv" for o in m.get("proposed_actions") or [])
               for m in stored)

    _stub(monkeypatch, "Offered below.", [{**offer, "id": "act_offer_2"}])
    again = _turn(engine, pid, "Add a PDF export", thread_id=out["thread_id"])
    msg2 = [m for m in again["messages"] if m["role"] == "assistant"][-1]["id"]
    with world.acting_as(world.ctx(world.owner)):
        no = client.post("/v1/engine/agent/proposal", json={
            "project_id": pid, "thread_id": again["thread_id"], "message_id": msg2,
            "outcome": "dismissed"})
        wrong = client.post("/v1/engine/agent/proposal", json={
            "project_id": pid, "thread_id": again["thread_id"], "message_id": "amsg_nope",
            "outcome": "dismissed"})
    assert no.status_code == 200 and no.json()["proposal"]["state"] == "dismissed"
    assert wrong.status_code == 404
