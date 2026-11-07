"""Project › Settings reads and writes like screen 13 (ACCEPTANCE 13-01 … 13-35, contract §11).

The seeded HelpDesk AI Demo (apps/api/tests/project_experience_seed.py, seed_settings_story) is
read through GET /v1/projects/{id}/settings and changed through the routes the page sends:

  * demo logins per environment (Staging / Preview / Prod) with the users' source in words;
  * ONE row per deploy value, in words ("BOSS manages · per env", "you set · missing in
    staging", "you set · rotated 12d ago") — DEMO_PASSWORD was listed once per app key (PX-07);
  * Staging "docker · port 8004" on its server, Production "no host yet · Pick host";
  * the project's autonomy level (A0 Ask … A3 Ship) saved for every task class, and the week's
    ambient budget, spent / cap (PX-22);
  * who is told about what: Needs you / Incidents / PR ready → channels, editable, and the
    monitor's tick queues those messages for the routed channels only;
  * Rotate: a new generated demo password for one environment — owner/admin with a fresh
    step-up, audited, never returned.

Real Postgres; no model, no GitHub, no host.
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
    w.owner = w.person("set-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("set-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-ai-demo-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _sections(world, client, pid, who=None):
    with world.acting_as(world.ctx(who or world.owner)):
        r = client.get(f"/v1/projects/{pid}/settings")
    assert r.status_code == 200, r.text
    return {s["key"]: s for s in r.json()["sections"]}


def test_every_section_reads_in_words_like_the_wireframe(world, client, helpdesk):
    s = _sections(world, client, helpdesk["id"])
    assert [x["label"] for x in s.values()] == ["Demo logins", "Deploy values", "Targets & hosts",
                                                "Captain & squad", "Autonomy & budget", "Repos",
                                                "Notifications"]
    demo = s["demo_logins"]
    assert demo["users_from_words"] == "boss.deploy.yaml"
    assert [e["label"] for e in demo["environments"]] == ["Staging", "Preview", "Prod"]
    staging = demo["environments"][0]
    assert [x["user"] for x in staging["logins"]] == ["customer@demo.test", "agent@demo.test",
                                                      "admin@demo.test"]
    assert staging["rotate"]["href"].endswith("/demo-logins/rotate?env=staging")
    assert demo["environments"][2]["logins"] == [] and demo["environments"][2]["rotate"] is None

    values = {v["name"]: v for v in s["deploy_values"]["items"]}
    assert [n for n in values].count("DEMO_PASSWORD") == 1
    assert values["DEMO_PASSWORD"]["words"] == "BOSS manages · per env"
    assert values["DEMO_PASSWORD"]["tag"] == "generated"
    assert values["DATABASE_URL"]["words"] == "BOSS manages · staging"
    assert values["OPENAI_API_KEY"]["state"] == "you"
    assert values["OPENAI_API_KEY"]["words"] == "you set · missing in staging"
    assert values["OPENAI_API_KEY"]["actions"][0]["label"] == "Set"
    assert values["SMTP_URL"]["words"] == "you set · rotated 12d ago"
    assert {a["label"] for a in values["SMTP_URL"]["actions"]} == {"Rotate", "Remove"}

    rows = [i for i in s["targets"]["items"] if i["kind"] in ("target", "missing")]
    assert [(r["env_label"], r["words"], r["state"]) for r in rows] == [
        ("Staging", "docker · port 8004", "done"), ("Production", "no host yet", "you")]
    assert rows[1]["action"] == {"label": "Pick host",
                                 "open": f"?project_id={helpdesk['id']}&area=infra&add=target"}
    assert rows[0]["host_href"] == f"?project_id={helpdesk['id']}&area=infra&host={helpdesk['host_id']}"

    assert "view=org" in s["captain"]["squad_href"]
    auto = s["autonomy"]
    assert auto["level"] == "A1"
    assert [lv["label"] for lv in auto["levels"]] == ["A0 Ask", "A1 Propose", "A2 Act", "A3 Ship"]
    assert auto["ambient"]["week_cap_usd"] == pytest.approx(50.0, abs=0.01)
    assert auto["ambient"]["week_spent_usd"] == pytest.approx(14.0)
    assert s["repos"]["items"][0]["access_words"] == "access ✓"
    routes = {r["event"]: r["words"] for r in s["notifications"]["routes"]}
    assert routes == {"needs_you": "Email · Slack", "incidents": "Email · Slack", "pr_ready": "Email"}


def test_the_project_level_is_saved_for_every_class_a_viewer_cannot(world, client, helpdesk, engine):
    pid = helpdesk["id"]
    with world.acting_as(world.ctx(world.viewer)):
        assert client.put(f"/v1/projects/{pid}/autonomy", json={"level": "A2"}).status_code == 403
    with world.acting_as(world.ctx(world.owner)):
        r = client.put(f"/v1/projects/{pid}/autonomy", json={"level": "A2"})
        bad = client.put(f"/v1/projects/{pid}/autonomy", json={"level": "A9"})
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "A2"
    assert bad.status_code == 422
    with engine.connect() as db:
        changes = db.execute(text(
            "SELECT count(*) FROM ambient_autonomy_changes WHERE project_id = :p AND to_level = 'A2'"),
            {"p": pid}).scalar()
    assert changes >= 1
    assert _sections(world, client, pid)["autonomy"]["level"] == "A2"


def test_who_is_told_is_routed_and_the_tick_queues_only_routed_channels(world, client, helpdesk,
                                                                         engine):
    from boss_engine.config import get_settings
    from boss_engine.services.project_experience import notify_routes

    pid = helpdesk["id"]
    cps = helpdesk["settings"]["contact_points"]
    with world.acting_as(world.ctx(world.owner)):
        r = client.put(f"/v1/projects/{pid}/notification-routes/pr_ready",
                       json={"contact_point_ids": [cps["email"], cps["slack"]]})
        incidents = client.put(f"/v1/projects/{pid}/notification-routes/incidents",
                               json={"contact_point_ids": [cps["slack"]]})
        unknown = client.put(f"/v1/projects/{pid}/notification-routes/needs_you",
                             json={"contact_point_ids": ["cp_nope"]})
    assert r.status_code == 200, r.text
    assert {x["event"]: x["words"] for x in r.json()["routes"]}["pr_ready"] == "Email · Slack"
    assert incidents.status_code == 200
    with engine.connect() as db:
        root = db.execute(text("SELECT contact_point_ids FROM notification_policies WHERE "
                               "project_id = :p AND parent_id IS NULL"), {"p": pid}).scalar()
    assert root == [cps["slack"]]
    assert unknown.status_code == 422

    with engine.begin() as db:
        first = notify_routes.plan(db, get_settings(), pid)
    with engine.begin() as db:
        again = notify_routes.plan(db, get_settings(), pid)
        queued = db.execute(text(
            "SELECT contact_point_id, payload->>'event' AS ev FROM notifications WHERE project_id = :p"),
            {"p": pid}).mappings().all()
    assert first["needs_you"] >= 2 and again == {"needs_you": 0, "pr_ready": 0}
    assert {q["contact_point_id"] for q in queued} <= {cps["email"], cps["slack"]}
    assert {q["ev"] for q in queued} >= {"needs_you"}


def test_rotate_needs_a_fresh_step_up_is_audited_and_never_returns_a_value(world, client,
                                                                           helpdesk, engine,
                                                                           monkeypatch):
    import base64

    # The vault seals the new app-binding value with the install's master key (a test one here).
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"r" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    from boss_engine.config import get_settings
    from boss_engine.services.stack_deploy import ensure_generated_secret
    from boss_engine.templates.secrets import target_demo_logins

    pid = helpdesk["id"]
    before = ensure_generated_secret(engine, get_settings(), project_id=pid,
                                     target_id=helpdesk["target_id"], stack="helpdesk-ai-demo",
                                     key="DEMO_PASSWORD", generator="password", actor="test")
    path = f"/v1/projects/{pid}/demo-logins/rotate"
    with world.acting_as(world.ctx(world.viewer)):
        assert client.post(path, params={"env": "staging"}).status_code == 403
    with world.acting_as(world.ctx(world.owner, mfa=False)):
        stale = client.post(path, params={"env": "staging"})
    assert stale.status_code == 401 and stale.json().get("error") == "step_up_required"
    with world.acting_as(world.ctx(world.owner)):
        done = client.post(path, params={"env": "staging"})
        missing = client.post(path, params={"env": "production"})
    assert done.status_code == 200, done.text
    assert done.json()["rotated"] >= 1 and done.json()["redeploy"] is True
    assert before not in done.text and '"password"' not in done.text
    assert missing.status_code == 404
    with engine.connect() as db:
        target = dict(db.execute(text(
            "SELECT t.id, t.environment, s.manifest FROM deployment_targets t JOIN stack_deployments s "
            "ON s.target_id = t.id AND s.status = 'healthy' WHERE t.id = :t "
            "ORDER BY s.created_at DESC LIMIT 1"), {"t": helpdesk["target_id"]}).mappings().first())
        after = {r["value"] for r in target_demo_logins(db, get_settings(), project_id=pid,
                                                         target=target)}
        audited = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
            "'project.demo_logins_rotated'"), {"p": pid}).scalar()
    assert after and before not in after
    assert audited == 1
