"""Screen 10's "Accept backlog / Edit" and screen 07's Plan lanes (BOSSAPP-913).

The onboarding agent proposes a ranked backlog. A person accepts it — after reordering, dropping
and renaming in Edit — and the chosen items land on the Roadmap's Next lane as accepted items,
in the order given. NOTHING STARTS: no workflow run and no build exist after the accept. Items
move between Next and Later; starting one through THE start door takes it off the lanes.
Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("plan-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("plan-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine, monkeypatch):
    # The Health tab's alert history is not this file's subject (and is seeded on its own).
    from apps.api.tests import project_experience_seed as px_seed

    monkeypatch.setattr(px_seed, "seed_alert_history", lambda *a, **k: {})
    slug = f"plan-demo-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _as(world, who):
    return world.acting_as(world.ctx(who))


def _runs(engine, pid):
    with engine.connect() as c:
        return (c.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                          {"p": pid}).scalar(),
                c.execute(text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
                          {"p": pid}).scalar())


def test_the_onboarding_view_has_the_wireframe_stages_rows_and_full_backlog(world, client, project):
    with _as(world, world.owner):
        ob = client.get(f"/v1/projects/{project['id']}/onboarding").json()
    assert [s["label"] for s in ob["stages"]] == ["Discover", "Ready for work", "Propose backlog",
                                                  "Work it", "Deploy"]
    assert [r["label"] for r in ob["ready"]][:6] == ["Repo access", "Builds clean", "Tests run",
                                                     "Workflow picked", "Staging host",
                                                     "App secrets"]
    assert all(r.get("agent") for r in ob["ready"])
    assert ob["backlog_total"] == 12 and len(ob["backlog"]) == 12
    assert ob["accept_backlog"]["href"].endswith("/onboarding/backlog/accept")
    # The seed accepted five through the real path; the view marks them.
    assert len(ob["accepted"]) == 5
    assert sum(1 for b in ob["backlog"] if b.get("state") == "accepted") == 5


def test_accepting_the_backlog_puts_items_on_next_in_order_and_starts_nothing(world, client,
                                                                             project, engine):
    pid = project["id"]
    with _as(world, world.owner):
        ob = client.get(f"/v1/projects/{pid}/onboarding").json()
    fresh = [b for b in ob["backlog"] if b.get("state") != "accepted"]
    picked = [fresh[2]["id"], fresh[0]["id"]]          # Edit: reordered, the rest dropped
    before = _runs(engine, pid)
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{pid}/onboarding/backlog/accept",
                        json={"ids": picked, "titles": {fresh[2]["id"]: "Renamed first"}})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == picked and r.json()["started"] == []
    assert _runs(engine, pid) == before                 # nothing started
    with _as(world, world.owner):
        lanes = client.get(f"/v1/projects/{pid}/plan").json()
    titles = [c["title"] for c in lanes["next"]]
    assert titles[-2:] == ["Renamed first", fresh[0]["label"]]
    assert all(c["start"]["body"]["source_ref"].startswith("rec:") for c in lanes["next"])
    # Accepting again is idempotent: nothing new, nothing duplicated.
    with _as(world, world.owner):
        again = client.post(f"/v1/projects/{pid}/onboarding/backlog/accept", json={"ids": picked})
    assert again.json()["accepted"] == [] and set(again.json()["already"]) == set(picked)
    with _as(world, world.owner):
        assert len(client.get(f"/v1/projects/{pid}/plan").json()["next"]) == len(lanes["next"])


def test_the_plan_has_now_next_later_and_cards_move_between_lanes(world, client, engine, project):
    pid = project["id"]
    # The deploy-waiting run is the accepted "Email-to-ticket intake" already moving.
    with engine.begin() as db:
        db.execute(text(
            "UPDATE workflow_runs SET input = jsonb_set(input, '{title}', "
            "'\"Email-to-ticket intake\"') WHERE id = :r"), {"r": project["deploy"]["run_id"]})
    with _as(world, world.owner):
        lanes = client.get(f"/v1/projects/{pid}/plan").json()
    assert [c["title"] for c in lanes["later"]] == ["Reply suggestions", "Status index"]
    # ONE LANE PER ITEM (PX-38): "Email-to-ticket intake" was accepted, but its run is already
    # moving, so it shows once, in Now — never in Now and Next at once.
    assert [c["title"] for c in lanes["next"]] == ["SLA timer ignores weekends", "Fix 6 failing tests"]
    assert "Email-to-ticket intake" in [c["label"] for c in lanes["now"]]
    assert {c["state"] for c in lanes["now"]} <= {"running", "needs_you", "stalled"} and lanes["now"]
    first = lanes["next"][0]
    with _as(world, world.owner):
        moved = client.post(f"/v1/projects/{pid}/plan/{first['id']}",
                            json={"lane": "later", "before": lanes["later"][0]["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["later"][0]["id"] == first["id"]
    assert first["id"] not in [c["id"] for c in moved.json()["next"]]
    with _as(world, world.owner):
        bad = client.post(f"/v1/projects/{pid}/plan/{first['id']}", json={"lane": "someday"})
    assert bad.status_code == 422


def test_a_viewer_reads_the_plan_but_cannot_accept_or_move(world, client, project):
    pid = project["id"]
    with _as(world, world.viewer):
        assert client.get(f"/v1/projects/{pid}/plan").status_code == 200
        ob = client.get(f"/v1/projects/{pid}/onboarding").json()
        r = client.post(f"/v1/projects/{pid}/onboarding/backlog/accept",
                        json={"ids": [ob["backlog"][0]["id"]]})
    assert r.status_code == 403


def test_an_unknown_candidate_is_refused_in_words(world, client, project):
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{project['id']}/onboarding/backlog/accept",
                        json={"ids": ["cand_nope"]})
    assert r.status_code == 404 and "Not in the proposed backlog" in r.text


def test_starting_a_planned_item_takes_it_off_the_lanes(world, client, project, engine):
    from boss_engine.services.project_experience import plan

    pid = project["id"]
    with _as(world, world.owner):
        card = client.get(f"/v1/projects/{pid}/plan").json()["next"][0]
    with engine.begin() as db:
        run = db.execute(text("SELECT id FROM workflow_runs WHERE project_id = :p LIMIT 1"),
                         {"p": pid}).scalar()
        assert plan.link_started(db, pid, card["start"]["body"]["source_ref"], run)
        assert not plan.link_started(db, pid, "issue:9", run)
    with _as(world, world.owner):
        lanes = client.get(f"/v1/projects/{pid}/plan").json()
    assert card["id"] not in [c["id"] for c in lanes["next"] + lanes["later"]]


def test_the_projects_list_says_not_onboarded_unless_an_onboarding_job_is_moving(world, client,
                                                                                 engine):
    from apps.api.tests.project_experience_seed import shape_more_projects

    ids = []
    for name in ("NorthPeak Retail", "Hello World"):
        slug = f"pl-{uuid.uuid4().hex[:6]}"
        with _as(world, world.owner):
            assert client.post("/v1/projects", json={"name": name, "slug": slug}).status_code == 201
        ids.append(slug)
    with engine.begin() as db:
        shape_more_projects(db, ids[0], ids[1])
    with _as(world, world.owner):
        body = client.get("/v1/projects/summary").json()
    items = {i["id"]: i for i in body["items"]}
    live, never = items[ids[0]], items[ids[1]]
    assert live["onboarding_active"] is True and live["status_label"] == "Onboarding"
    assert live["onboarding_progress"][0] == 1 and live["lines"][0] == "mapping code"
    assert never["onboarding_active"] is False and never["status_label"] == "Not onboarded"
    assert never["start_onboarding"]["href"].endswith("/onboarding/start")
    # 02-16 the crew is always drawn; nobody on it is working on a project that never started.
    assert never["lines"] == ["not deployed yet", "idle"] and never["live_agents"] == []
    assert all(c["state"] == "idle" for c in never["crew"])


def test_a_project_card_carries_its_ring_lines_and_live_crew(world, client, project):
    with _as(world, world.owner):
        card = next(i for i in client.get("/v1/projects/summary").json()["items"]
                    if i["id"] == project["id"])
    assert card["uptime_pct"] == 100.0                  # 30 probe samples, all answered
    assert card["lines"][0] == "staging healthy"
    assert card["lines"][1].startswith("1 building") or "merged" in card["lines"][1]
    # 02-16 one face per job from the real roster; `working` only on who acts right now
    # (live_agents, the Running tile's faces), never painted on the whole crew.
    assert card["crew"] and {c["state"] for c in card["crew"]} <= {"working", "idle"}
    live = {g["role_key"] for g in card["live_agents"]}
    assert all((c["state"] == "working") == (c["role_key"] in live) for c in card["crew"])
