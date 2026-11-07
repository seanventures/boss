"""Project experience, tester round 2 fixes (px-r4), the parent slice, fixed as classes:

* 05-04 a kind label is not a name: "Bug: weekend tickets breach SLA because…" starts work
  titled "Weekend tickets breach SLA", never "Bug" (titles.strip_label, the start door's title).
* 02-05 / 02-16 / 02-21 Projects cards: the crew is the project's REAL roster, one face per job
  (Plans · Builds · Tests · Deploys, whatever role keys the org uses), always drawn; the faces
  acting right now are `working` and are the Running tile's `live_agents`.
* 04-34 the captain's crew row: Marshal (Chief of Staff) closes it as `ranker`, out of the
  project's squad; each member's word is the one verb (Beacon "Deploys").
* 04-15 the cost tile carries `cap_pct` and says `first_week` when there is no delta.

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


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("r4-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"r4-{uuid.uuid4().hex[:6]}"
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


def test_a_bug_label_never_becomes_the_title():
    from apps.api.app.routers.project_experience import _title_of

    assert _title_of("Bug: weekend tickets breach SLA because timers count Saturdays",
                     "HelpDesk") == "Weekend tickets breach SLA"
    assert _title_of("Feature: export tickets to CSV", "HelpDesk") == "Export tickets to CSV"


def test_cards_draw_the_real_crew_and_who_is_acting_now(world, client, engine, helpdesk):
    from boss_engine.services.project_experience.cards import CREW_SLOTS

    with engine.begin() as db:
        roster = {str(k) for k in db.execute(text(
            "SELECT role_key FROM roles WHERE project_id = :p"), {"p": helpdesk["id"]}).scalars()}
        db.execute(text("UPDATE agent_acts SET ended_at = COALESCE(ended_at, now()), "
                        "status = 'complete' WHERE project_id = :p"), {"p": helpdesk["id"]})
    card = next(c for c in _get(world, client, "/v1/projects/summary")["items"]
                if c["id"] == helpdesk["id"])
    live_now = {g["role_key"] for g in card["live_agents"]}
    # Each job's face: the one acting now (the agent on a live run's step), else the roster's first.
    expected = [next((k for k in slot if k in roster and k in live_now), None)
                or next(k for k in slot if k in roster) for slot in CREW_SLOTS
                if any(k in roster for k in slot)]
    assert [g["role_key"] for g in card["crew"]] == expected and expected
    # With no open act, a live run's face is the agent of the step it is on (round 3, 02-05:
    # a Running count is never drawn without its faces); the crew says the same.
    live = {g["role_key"] for g in card["live_agents"]}
    assert card["running"] and live
    assert all((g["state"] == "working") == (g["role_key"] in live) for g in card["crew"])
    tester = next(k for k in CREW_SLOTS[2] if k in roster)
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO agent_acts (act_id, root_act_id, project_id, role_key, kind, status, "
            "handed_by_kind, how, accountable, label, workflow_run_id, started_at) VALUES "
            "(:a, :a, :p, :r, 'step', 'active', 'boss', 'next_stage', 'ceo', 'Testing', :w, "
            "now())"),
            {"a": f"act_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"], "r": tester,
             "w": helpdesk["running"]["run_id"]})
    card = next(c for c in _get(world, client, "/v1/projects/summary")["items"]
                if c["id"] == helpdesk["id"])
    assert tester in [g["role_key"] for g in card["live_agents"]]
    assert {g["role_key"]: g["state"] for g in card["crew"]}[tester] == "working"


def test_marshal_closes_the_crew_row_and_each_member_has_one_verb(world, client, helpdesk):
    cap = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")["captain"]
    assert cap["ranker"]["role_key"] == "chief_of_staff" and cap["ranker"]["role"] == "Ranks"
    assert cap["ranker"]["title"] == "Chief of Staff ranks this project's backlog against the others"
    assert "chief_of_staff" not in [m["role_key"] for m in cap["squad"]]
    words = {m["role_key"]: m["role"] for m in cap["squad"]}
    if "sre" in words:
        assert words["sre"] == "Deploys"


def test_the_cost_tile_carries_its_cap_share_and_first_week(world, client, engine, helpdesk):
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
            "VALUES (:i, 'project', :p, 10000000, 0, 0)"),
            {"i": f"bp_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"]})
    tile = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")["tiles"]["cost_week"]
    assert tile["cap_usd"] == 70.0
    assert tile["cap_pct"] == round(tile["usd"] * 100 / 70.0)
    assert "first_week" in tile and "list_delta_pct" in tile
    if tile["delta_pct"] is None and tile["usd"] > 0 and not tile["on_subscription"]:
        assert tile["first_week"] is True


def test_an_approved_plan_names_the_agent_who_wrote_it(world, client, engine, helpdesk):
    """06-14 "Juno · approved Nov 3": the plan's writer agent, then approved and the day; the
    person who approved it rides in approved_by, never in the row's words."""
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
            "approved_at, created_at) VALUES (:i, :p, 'Approve the plan for SLA timers?', "
            "CAST(:r AS JSONB), 'approved', 'ceo', now(), now())"),
            {"i": f"dec_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"],
             "r": '{"capability": "spec_review"}'})
    kn = _get(world, client, f"/v1/projects/{helpdesk['id']}/knowledge")
    plans = [d for d in kn["decisions"] if d["label"].startswith("Plan")]
    assert plans, kn["decisions"]
    sub = plans[0]["sub"]
    assert " · approved " in sub and not sub.startswith("Approved")
    assert plans[0]["agent"] and sub.startswith(plans[0]["agent"]["name"])
    assert plans[0]["approved_by"]
