"""Project experience, tester round 3 fixes (px-r5), each at its cause, each as a class:

* 07-10 / X-74 a proposal about another run names it by its SHORT title, in quotes — never a cut
  of its brief in brackets — and rows written the old way are re-derived on read.
* X-76 a diagram label is at most four words.
* 10-07 the onboarding strip keeps a later stage that is done (Deploy after four deploys).
* 05-12 / 05b-09 the D station and the drawer's "<who> approved" come from a person's decision.
* 00a-02/13/34 the install's untouched "Default project" is not a project.
* 02-05 / 02-11/14 / 02-13/14 one "what moves now" and one environment per card.
* 04-02..05, 04-36/07-03 the journey's day is sent as a moment; Ready is the Ready card's
  verdict; the Backlog list is the items its count counts; What's next is the plan's Next lane.
* 07-20 every recommendation card has an evidence node; 11-08 ⌘K offers the live app;
  14-05/14-10 the server's counts and its recent changes, who shipped which PR.

Real Postgres and the HelpDesk seed; no model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


# ── pure rules ─────────────────────────────────────────────────────────────────────────────

OLD_WHY = ("Review findings deferred from PR #3 (Finish the visual refresh the last change "
           "promised but did not deliver. The tick) were never fixed.")


def test_a_proposal_names_another_run_by_its_short_title():
    from boss_engine.services.project_experience import onboarding
    from boss_engine.services.project_experience.titles import run_name

    name = run_name("Finish the visual refresh the last change promised but did not deliver. The tick")
    said = onboarding.deferred_words(1, 3, name)
    assert said["detail"] == ("Review findings deferred from PR #3 on “Finish the visual refresh” "
                              "were never fixed.")
    # No nested brackets from the other title, no cut sentence.
    assert run_name("Add SLA timers (P1) to tickets") == "Add SLA timers to tickets"
    built = onboarding.unfinished_words(run_name("Add SLA timers to tickets: each priority"), "blocked")
    assert built["label"] == "Finish add SLA timers to tickets"
    assert "“Add SLA timers to tickets”" in built["detail"]


def test_rows_written_the_old_way_read_right_without_a_re_run():
    from boss_engine.services.project_experience.onboarding import readable

    title, why = readable("Fix 1 deferred review finding", OLD_WHY)
    assert title == "Fix 1 deferred review finding"
    assert why == "Review findings deferred from PR #3 on “Finish the visual refresh” were never fixed."
    assert "(" not in why and "tick" not in why.lower()
    t2, w2 = readable("Finish: Add SLA timers to tickets: each priority gets",
                      "The build “Add SLA timers to tickets: each priority gets” ended blocked "
                      "and no later attempt completed.")
    assert t2.startswith("Finish ") and ":" not in t2
    assert w2 == "The build “Add SLA timers to tickets” ended blocked and no later attempt completed."
    # Anything else is returned as stored.
    assert readable("Add CI", "No CI on the default branch.") == ("Add CI", "No CI on the default branch.")


def test_a_diagram_label_is_at_most_four_words():
    from boss_engine.services.project_experience.knowledge import four_words, node_label

    assert four_words("The app has no test script") == "No test script"
    assert four_words("Ticket creation succeeds when the db is up") == "Ticket creation succeeds"
    assert four_words("Make npm test pass") == "Make npm test pass"
    assert four_words("SLA timers drift outside business hours") == "SLA timers drift"
    for kind, name in (("test_failure", "npm test fails: The app has no test script at all"),
                       ("rule", "Tickets breach their SLA when the weekend clock keeps counting")):
        assert len(node_label(kind, name, None, {}).split()) <= 4


def test_a_later_stage_that_is_done_stays_done():
    from boss_engine.services.project_experience.onboarding import linear_stages

    got = linear_stages([{"key": "discover", "state": "done"}, {"key": "ready", "state": "now"},
                         {"key": "backlog", "state": "done"}, {"key": "first_work", "state": "todo"},
                         {"key": "deploy", "state": "done"}], "needs_you")
    # The app live on staging is a fact; a backlog drafted before Ready is not ticked.
    assert [s["state"] for s in got] == ["done", "now", "todo", "todo", "done"]
    # Before onboarding starts nothing is current and nothing is done.
    assert {s["state"] for s in linear_stages([{"key": "deploy", "state": "done"}],
                                              "not_started")} == {"todo"}


def test_the_d_station_is_the_persons_decision_on_the_plan():
    from boss_engine.services.project_experience.work import _decided

    strip = [{"key": "plan", "state": "done"}, {"key": "design", "state": "skipped"},
             {"key": "build", "state": "done"}]
    approved = {"meeting": {"status": "succeeded", "output": {"spec_decision_id": "dec_1",
                                                              "spec_approved_by": "ceo"}}}
    assert _decided(strip, approved)[1]["state"] == "done"
    waiting = {"meeting": {"status": "waiting", "gate_open": False,
                           "output": {"spec_decision_id": "dec_1"}}}
    held = _decided([{"key": "plan", "state": "now"}, *strip[1:]], waiting)
    assert [s["state"] for s in held[:2]] == ["done", "waiting"]
    # An auto-approved PRD (BOSS never waited on a person): D stays skipped.
    assert _decided(strip, {"meeting": {"status": "succeeded", "output": {}}})[1]["state"] == "skipped"


def test_the_bootstrap_default_project_is_not_a_project():
    from boss_engine.config import get_settings
    from boss_engine.services.project_experience.overview import is_placeholder

    default = get_settings().default_project_id
    assert is_placeholder(default, repo=None, worked=False, targets=False, onboarded=False)
    assert not is_placeholder(default, repo="acme/app", worked=False, targets=False,
                              onboarded=False)
    assert not is_placeholder(default, repo=None, worked=True, targets=False, onboarded=False)
    assert not is_placeholder("someone-elses", repo=None, worked=False, targets=False,
                              onboarded=False)


# ── on the seeded project ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("r5-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"r5-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _get(world, client, path):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_overview_reads_one_truth(world, client, helpdesk):
    pid = helpdesk["id"]
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    nodes = {n["key"]: n for n in ov["journey"]}
    # 04-02: the moment, never a server-side day.
    assert nodes["onboarding"]["at"] and nodes["onboarding"]["sub"] == ""
    assert "{day}" in nodes["onboarding"]["detail"]
    # 04-03: Ready is ✓ only when the Ready card says every row is done.
    onb = _get(world, client, f"/v1/projects/{pid}/onboarding")
    ready_stage = next(s for s in onb["stages"] if s["key"] == "ready")
    assert (nodes["ready"]["state"] == "done") == (ready_stage["state"] == "done")
    done = sum(1 for r in onb["ready"] if r["state"] in ("done", "skipped"))
    assert nodes["ready"]["sub"] == f"{done}/{len(onb['ready'])}"
    # 04-04: the Backlog list is the very items its count counts, each once.
    ids = [b["id"] for b in ov["backlog"]]
    assert len(ids) == len(set(ids)) == ov["roadmap_count"]
    # 04-05: work waiting on a person is a decision on a piece of work, never a setup card.
    people = [n for n in ov["needs_you"] if n.get("work_id") and not n["id"].startswith("onb:")]
    assert (f"{len(people)} waiting on you" in nodes["working"]["detail"]) == bool(people)
    # 04-36 / 07-03: What's next leads with the plan's Next lane, accepted, without an Accept.
    plan = _get(world, client, f"/v1/projects/{pid}/plan")
    lane = [c["id"] for c in plan["next"]][:3]
    assert [n["id"] for n in ov["next"] if n.get("accepted")] == lane
    assert all(n["accept"] is None for n in ov["next"] if n.get("accepted"))


def test_an_accepted_twin_is_one_item(world, client, engine, helpdesk):
    pid = helpdesk["id"]
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT status FROM ambient_recommendations WHERE project_id = :p AND "
            "title = 'Email-to-ticket intake'"), {"p": pid}).scalars().all()
    assert list(rows) == ["accepted"]


def test_a_work_row_lights_d_and_the_drawer_names_who_approved(world, client, helpdesk):
    pid = helpdesk["id"]
    work = _get(world, client, f"/v1/projects/{pid}/work")["items"]
    done = next(w for w in work if w["source"] == "workflow_run" and w["state"] == "done")
    d = next(s for s in done["stages"] if s["key"] == "design")
    assert d["state"] == "done"
    story = _get(world, client, f"/v1/projects/{pid}/work/{done['id']}/story")
    rows = {s["key"]: s for s in story["stages"]}
    assert rows["design"]["person"] and rows["design"]["at"] and rows["design"]["sub"]
    assert rows["pr"]["sub"].endswith("approved · merged")


def test_a_projects_card_says_one_truth(world, client, helpdesk):
    from boss_engine.services.project_experience.health import target_states

    card = next(c for c in _get(world, client, "/v1/projects/summary")["items"]
                if c["id"] == helpdesk["id"])
    # 02-05: a live count always has its faces.
    assert card["running"] > 0 and card["live_agents"]
    # 02-11/14: the Working pill and the "N building" line read the same count.
    assert card["status_label"] == "Working" and f"{card['running']} building" in card["lines"][1]
    # 02-13/14: the ring is the line's environment's 24 h uptime.
    assert card["lines"][0].startswith("staging")
    assert card["uptime_pct"] is not None


def test_every_recommendation_card_has_its_evidence(world, client, helpdesk):
    got = _get(world, client, f"/v1/ambient/roadmap?project_id={helpdesk['id']}&status=open&limit=30")
    assert got["items"]
    for item in got["items"]:
        assert item["card"]["evidence"], item["title"]
        for chip in item["card"]["evidence"]:
            assert len(chip["label"].split()) <= 4


def test_jump_offers_the_live_app(world, client, helpdesk):
    got = _get(world, client, f"/v1/jump?q=pr%203&project_id={helpdesk['id']}")["items"]
    live = [i for i in got if i["kind"] == "deploy" and i.get("external_url")]
    assert live and live[0]["hint"] == "open ↗" and live[0]["label"].startswith("Staging · ")
    dated = [i for i in got if i["kind"] == "deploy" and not i.get("external_url")]
    assert all(i.get("at") for i in dated)


def test_the_server_counts_and_changes_say_who_shipped_what(world, client, engine, helpdesk):
    from boss_engine.services.project_experience.host_overview import overview, usage

    pid, host = helpdesk["id"], helpdesk["host_id"]
    with engine.begin() as db:
        view = overview(db, pid, host, [pid])
        used = usage(db, [host], [pid])[host]
    assert view["capacity"]["ports"]["used"] == len(used["ports"])
    assert len(view["projects"]) == used["apps"]
    pr_rows = [c for c in view["changes"] if "PR #" in c["label"]]
    assert pr_rows and all(c["by"] for c in view["changes"])


def test_the_consent_sheet_names_the_rule_in_words(world, client, engine, helpdesk, monkeypatch):
    """X-73: switching Respond automatically on asks consent for "Error rate", never `AppErrorRate`."""
    from boss_engine.services import app_alerts

    asked: list[str] = []
    monkeypatch.setattr(app_alerts, "respond_link",
                        lambda connection, project_id, name: asked.append(name) or {})
    with engine.begin() as db:
        target = dict(db.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                                 {"t": helpdesk["target_id"]}).mappings().one())
        app_alerts.ensure_rules(db, target)
        rule = db.execute(text("SELECT id FROM app_alert_rules WHERE target_id = :t AND "
                               "kind = 'error_rate'"), {"t": target["id"]}).scalar()
        app_alerts.update_rule(db, target, rule, {"auto_respond": True, "triage": True},
                               actor="r5-owner")
    assert asked == ["Error rate"]
