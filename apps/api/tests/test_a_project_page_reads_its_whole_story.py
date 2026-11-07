"""The Project page reads a project's whole story, one round trip per tab (BOSSAPP-913 / 914).

Contract: docs/product/PROJECT_EXPERIENCE_CONTRACT.md. A HelpDesk-like project (3 merged PRs, a
running build, a PR to merge, a deploy to approve, a healthy staging deploy with demo users, two
roadmap items and a dossier) is created the way a person creates one and read through every tab:
Projects list, Overview, Work, Repo & knowledge, Deployments, Health, Captain, Settings and ⌘K.
A viewer reads every tab; a stranger reads none; only an owner with a fresh step-up sees a demo
password. Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import DEMO_USERS, REPO, STAGING_URL, seed

READS = ("overview", "work", "knowledge", "deployments", "health", "captain", "onboarding",
         "settings", "demo-logins")
TIMINGS: dict[str, float] = {}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("px-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("px-viewer")
    w.bind(w.viewer, "viewer")
    w.operator = w.person("px-operator")
    w.bind(w.operator, "operator")
    w.stranger = w.person("px-stranger")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    """Function-scoped: the suite's clean_tables resets the database before every test."""
    slug = f"helpdesk-ai-demo-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _get(world, client, who, path, **params):
    with world.acting_as(world.ctx(who)):
        started = time.perf_counter()
        r = client.get(path, params=params)
        TIMINGS[path.split("/")[-1] or path] = time.perf_counter() - started
    return r


# ── §1 the Projects list ────────────────────────────────────────────────────────────────────

def test_the_projects_list_shows_the_project_with_its_stage_crew_and_what_waits(world, client,
                                                                                 helpdesk):
    r = _get(world, client, world.owner, "/v1/projects/summary")
    assert r.status_code == 200, r.text
    card = next(i for i in r.json()["items"] if i["id"] == helpdesk["id"])
    assert card["name"] == "HelpDesk AI Demo" and card["initials"] == "HD"
    assert card["repo"] == REPO
    assert card["stage"] == "working"
    assert card["needs_you"] == 2 and card["running"] >= 1
    assert card["health"] == "healthy" and card["live_url"] == STAGING_URL
    assert card["captain"]["role_key"] == "engineering_captain"
    assert [j["key"] for j in card["journey"]] == ["onboarding", "ready", "backlog", "working",
                                                   "staging", "production"]
    assert {j["key"]: j["state"] for j in card["journey"]}["working"] in ("now", "waiting")
    assert card["href"] == f"?project_id={helpdesk['id']}&area=project"
    assert r.json()["first_run"] is False


def test_a_stranger_sees_no_card_for_a_project_they_cannot_read(world, client, helpdesk):
    r = _get(world, client, world.stranger, "/v1/projects/summary")
    assert r.status_code == 200, r.text
    assert helpdesk["id"] not in {i["id"] for i in r.json()["items"]}


# ── §2 Overview ─────────────────────────────────────────────────────────────────────────────

def test_the_overview_draws_the_journey_needs_you_tiles_captain_and_try_it(world, client,
                                                                            helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/overview")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["project"]["stage"] == "working" and o["project"]["status_label"] == "Working"
    assert o["project"]["repos"][0]["full_name"] == REPO
    assert o["project"]["repos"][0]["access"] == {"read": True, "write": True, "pr": True}
    assert o["project"]["live_url"] == STAGING_URL
    states = {j["key"]: j["state"] for j in o["journey"]}
    assert states["staging"] == "done" and states["production"] == "todo"
    decisions = [n for n in o["needs_you"] if n["action"]["decision_id"]]
    assert {n["kind"] for n in decisions} == {"merge", "deploy"}
    merge = next(n for n in decisions if n["kind"] == "merge")
    assert merge["label"] == "Merge PR #3" and merge["action"]["label"] == "Review PR"
    assert merge["action"]["href"].startswith(f"?project_id={helpdesk['id']}&area=decisions")
    # Short human labels (needs.py): "Deploy PR #5 to Staging" is the longest.
    assert all(len(n["label"].split()) <= 5 for n in o["needs_you"])
    tiles = o["tiles"]
    assert tiles["open_work"]["by_state"]["done"] == 3
    assert tiles["open_work"]["by_state"]["needs_you"] == 2
    assert tiles["running"]["count"] == 1
    assert tiles["running"]["current"]["label"] == "Ticket search filters"
    done, total = tiles["running"]["current"]["progress"]
    assert 0 <= done < total
    assert tiles["staging"]["health"] == "healthy" and tiles["staging"]["uptime_pct"] == 100.0
    assert len(tiles["cost_week"]["series"]) == 7
    assert o["captain"]["role_key"] == "engineering_captain"
    assert o["captain"]["ask"]["subject"] == {"kind": "project", "id": helpdesk["id"]}
    assert len(o["recent_work"]) == 5
    assert any(n["source"] == "roadmap" and n["accept"]["href"].endswith("/accept")
               for n in o["next"])
    assert o["try_it"]["url"] == STAGING_URL
    assert [d["user"] for d in o["try_it"]["demo_logins"]] == [u["user"] for u in DEMO_USERS]
    assert all(d["reveal"]["requires"] == "step_up" for d in o["try_it"]["demo_logins"])
    assert {e["kind"] for e in o["week"]} >= {"deploy", "merge"}


# ── §3 Work ─────────────────────────────────────────────────────────────────────────────────

def test_every_piece_of_work_is_a_row_with_its_stations_agents_cost_and_links(world, client,
                                                                               helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/work")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {w["id"]: w for w in body["items"]}
    merged = rows[helpdesk["merged"][0]["run_id"]]
    assert merged["state"] == "done" and merged["kind"] == "feature"
    assert [s["key"] for s in merged["stages"]] == ["plan", "design", "build", "review", "pr", "ship"]
    assert {s["key"]: s["state"] for s in merged["stages"]}["pr"] == "done"
    assert merged["links"]["pr"] == {"number": 1, "external_url": f"https://github.com/{REPO}/pull/1"}
    assert merged["links"]["build"] and merged["cost_usd"] == 2.1
    assert merged["links"]["workflow_run"] == (f"?project_id={helpdesk['id']}"
                                               f"&area=delivery_workflows&wfr={merged['id']}")
    running = rows[helpdesk["running"]["run_id"]]
    assert running["state"] == "running"
    assert "now" in {s["state"] for s in running["stages"]}
    waiting = rows[helpdesk["merge"]["run_id"]]
    assert waiting["state"] == "needs_you" and waiting["links"]["decision"]
    assert "waiting" in {s["state"] for s in waiting["stages"]}
    assert body["counts"] == {"running": 1, "stalled": 1, "needs_you": 2, "planned": 0, "done": 3,
                              "failed": 0}


def test_the_work_tab_filters_by_state_and_finds_by_words_or_pr_number(world, client, helpdesk):
    pid = helpdesk["id"]
    needs = _get(world, client, world.owner, f"/v1/projects/{pid}/work", state="needs_you").json()
    assert {w["state"] for w in needs["items"]} == {"needs_you"} and len(needs["items"]) == 2
    found = _get(world, client, world.owner, f"/v1/projects/{pid}/work", q="search").json()
    assert [w["title"] for w in found["items"]] == ["Ticket search filters"]
    by_pr = _get(world, client, world.owner, f"/v1/projects/{pid}/work", q="#2").json()
    assert [w["title"] for w in by_pr["items"]] == ["UI refresh"]
    assert needs["counts"]["done"] == 3       # counts are the whole project, not the filter


# ── §4 Repo & knowledge ─────────────────────────────────────────────────────────────────────

def test_repo_and_knowledge_shows_access_the_dossier_graph_and_docs(world, client, helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/knowledge")
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["repos"][0]["full_name"] == REPO and k["repos"][0]["access"]["pr"] is True
    d = k["dossier"]
    assert d["stack"][:2] == ["TypeScript", "Vite"] or "Vite" in d["stack"]
    assert d["commands"]["test"] is None or isinstance(d["commands"]["test"], str)
    assert d["tests"]["files"] == 2 and d["built_at"]
    assert k["graph"]["href"] == f"?project_id={helpdesk['id']}&area=knowledge"
    assert isinstance(k["graph"]["nodes"], list)
    assert any(doc["kind"] == "readme" for doc in k["docs"])


# ── §5 Deployments ──────────────────────────────────────────────────────────────────────────

def test_deployments_show_staging_with_health_last_deploy_and_who_to_sign_in_as(world, client,
                                                                                 helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/deployments")
    assert r.status_code == 200, r.text
    d = r.json()
    env = d["environments"][0]
    assert env["key"] == "staging" and env["label"] == "Staging"
    assert env["url"] == STAGING_URL and env["health"] == "healthy"
    assert env["host"] == "203.0.113.10" and env["policy"] == "person"
    assert env["last_deploy"]["commit"] == "f3f130c"
    assert [x["user"] for x in env["demo_logins"]] == [u["user"] for u in DEMO_USERS]
    assert env["demo_logins"][0]["reveal"]["href"] == (
        f"/v1/projects/{helpdesk['id']}/demo-logins/reveal?env=staging")
    # The server, in Infra (BOSSAPP-914 §5): its id and the host drawer's console link, and the
    # deploy target the Health tab's alert rules are read with.
    assert env["target_id"] == helpdesk["target_id"]
    assert env["host_id"] and env["host_href"] == (
        f"?project_id={helpdesk['id']}&area=infra&host={env['host_id']}")
    assert d["production"]["state"] == "none"
    assert d["production"]["action"]["label"] == "Set up production"
    # Infra opens its own + Add sheet from `add=` (it has no `targets` or `hosts` view).
    assert d["production"]["action"]["href"] == f"?project_id={helpdesk['id']}&area=infra&add=target"
    # How code ships: merge → build → staging (a person approves) → smoke → prod, a person.
    assert [n["label"] for n in d["ship_path"]] == ["PR merged", "Image built",
                                                    "Staging · you approve", "Smoke test",
                                                    "Prod · you approve"]
    assert [n["state"] for n in d["ship_path"]] == ["done", "done", "done", "done", "waiting"]
    assert all(len(n["label"].split()) <= 4 for n in d["ship_path"])
    assert "value" not in str(d) or "DEMO_PASSWORD" not in str(d).replace("password_key", "")


def test_a_server_in_infra_names_the_project_of_each_app_on_it(world, client, helpdesk):
    """§5's Infra cross-link: the host drawer's residents carry their project, so a shared
    server lists "Projects on this server", each linking to that Project › Deployments."""
    pid = helpdesk["id"]
    host = _get(world, client, world.owner, f"/v1/infra/hosts/{helpdesk['host_id']}",
                project_id=pid)
    assert host.status_code == 200, host.text
    app = next(r for r in host.json()["residents"] if r["kind"] == "target")
    assert app["project"] == {"id": pid, "name": "HelpDesk AI Demo",
                              "href": f"?project_id={pid}&area=project&tab=deployments"}
    fleet = _get(world, client, world.owner, "/v1/infra/fleet", project_id=pid).json()
    placed = [r for rack in fleet["racks"] for r in rack["residents"] if r["kind"] == "target"]
    assert placed and all(r["project"]["id"] == pid for r in placed)


# ── §12 Health ──────────────────────────────────────────────────────────────────────────────

def test_health_shows_each_environment_its_slo_and_how_monitoring_gets_set_up(world, client,
                                                                              helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/health")
    assert r.status_code == 200, r.text
    h = r.json()
    env = h["environments"][0]
    assert env["health"] == "healthy" and env["uptime_pct"] == 100.0
    assert env["slo"] == {"target": 99.5, "burn": "ok"}
    assert len(env["series"]["latency"]) == 30
    assert h["monitoring_pack"]["rules"] == 0 and h["monitoring_pack"]["managed_in"] == "none"
    assert "Beacon" in h["empty"]["detail"]
    assert h["links"]["observability"].startswith(f"?project_id={helpdesk['id']}&area=observability")
    assert env["target_id"] == helpdesk["target_id"]
    assert env["host"] == "203.0.113.10" and env["host_href"].startswith(
        f"?project_id={helpdesk['id']}&area=infra&host=")
    # Every console link names a view the console has (Observability has no rules/logs view).
    assert h["monitoring_pack"]["href"].endswith("area=observability&view=alerts")
    assert "view=logs" not in str(h) and "view=rules" not in str(h)
    assert h["alerts"] == [] and h["incidents"] == []


def test_a_firing_alert_and_the_squad_on_it_show_on_health(world, client, helpdesk, engine):
    pid = helpdesk["id"]
    wid = f"wi_{uuid.uuid4().hex[:12]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, alertname, "
            "labels, annotations, state, active_at, fired_at) VALUES (:i, :p, :t, 'error_rate', :f, "
            "'HighErrorRate', CAST(:l AS JSONB), CAST(:a AS JSONB), 'firing', now(), now())"),
            {"i": f"alr_{uuid.uuid4().hex[:10]}", "p": pid, "t": helpdesk["target_id"],
             "f": uuid.uuid4().hex, "l": '{"severity": "critical"}',
             "a": '{"summary": "5xx on /api/tickets"}'})
        db.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            "lifecycle, task_class, created_by) VALUES (:i, :p, 'incident', :i, "
            "'helpdesk 5xx 6.6%', true, 'in_progress', 'incident_staging_mitigation', 'test')"),
            {"i": wid, "p": pid})
    h = _get(world, client, world.owner, f"/v1/projects/{pid}/health").json()
    alert = h["alerts"][0]
    assert alert["label"] == "5xx on /api/tickets" and alert["severity"] == "page"
    assert alert["state"] == "firing"
    incident = next(i for i in h["incidents"] if i["id"] == wid)
    assert incident["state"] == "running"
    assert incident["graph_href"] == f"?project_id={pid}&area=agent_ops&item={wid}"
    assert [m["label"] for m in incident["mini"]] == ["Alert", "Triage", "Fix", "Verify"]
    with engine.begin() as db:
        db.execute(text("DELETE FROM app_alerts WHERE project_id = :p"), {"p": pid})
        db.execute(text("DELETE FROM work_items WHERE id = :i"), {"i": wid})


# ── §7 the captain ──────────────────────────────────────────────────────────────────────────

def test_a_new_project_is_captained_by_its_engineering_captain(world, client, helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/captain")
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["role_key"] == "engineering_captain" and c["chosen"] == "chosen"
    assert c["avatar"]["kit"] == "boss-v1"
    assert all(s["role_key"] != "engineering_captain" for s in c["squad"])
    assert any(a["role_key"] == "chief_of_staff" for a in c["candidates"]) or c["candidates"]


def test_an_owner_changes_the_captain_and_a_viewer_cannot(world, client, helpdesk):
    pid = helpdesk["id"]
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post(f"/v1/projects/{pid}/captain", json={"agent_id": "chief_of_staff"})
    assert refused.status_code == 403, refused.text
    with world.acting_as(world.ctx(world.owner)):
        wrong = client.post(f"/v1/projects/{pid}/captain", json={"agent_id": "nobody_here"})
        assert wrong.status_code == 422 and wrong.json()["error"] == "not_an_agent"
        changed = client.post(f"/v1/projects/{pid}/captain", json={"agent_id": "chief_of_staff"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["role_key"] == "chief_of_staff"
        back = client.post(f"/v1/projects/{pid}/captain", json={"agent_id": "engineering_captain"})
        assert back.json()["role_key"] == "engineering_captain"


# ── §9 jump ─────────────────────────────────────────────────────────────────────────────────

def test_jump_finds_a_pr_by_number_a_run_by_words_and_the_captain_by_name(world, client,
                                                                          helpdesk):
    pid = helpdesk["id"]
    pr = _get(world, client, world.owner, "/v1/jump", q="#3", project_id=pid).json()["items"]
    assert any(i["kind"] == "pr" and i["label"].startswith("PR #3") for i in pr)
    run = _get(world, client, world.owner, "/v1/jump", q="search", project_id=pid).json()["items"]
    assert any(i["kind"] == "run" and i["label"] == "Ticket search filters" for i in run)
    proj = _get(world, client, world.owner, "/v1/jump", q="HelpDesk").json()["items"]
    assert any(i["kind"] == "project" and i["project_id"] == pid for i in proj)
    hidden = _get(world, client, world.stranger, "/v1/jump", q="x", project_id=pid)
    assert hidden.status_code == 404


# ── §11 settings and demo logins ────────────────────────────────────────────────────────────

def test_settings_hold_every_project_setting_with_the_existing_write_as_its_action(world, client,
                                                                                   helpdesk):
    r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/settings")
    assert r.status_code == 200, r.text
    sections = {s["key"]: s for s in r.json()["sections"]}
    assert list(sections) == ["demo_logins", "deploy_values", "targets", "captain", "autonomy",
                              "repos", "notifications"]
    demo = sections["demo_logins"]
    assert [i["user"] for i in demo["items"]] == [u["user"] for u in DEMO_USERS]
    assert demo["actions"][0]["requires"] == "step_up"
    assert demo["actions"][0]["href"].endswith("/demo-logins/reveal?env=staging")
    assert sections["captain"]["actions"][0]["href"] == f"/v1/projects/{helpdesk['id']}/captain"
    assert any(i["kind"] == "target" and i["env"] == "staging" for i in sections["targets"]["items"])
    assert sections["repos"]["items"][0]["full_name"] == REPO
    # Every action names its write route AND the console screen where a person makes it (`open`),
    # never an API route as a link; items that live elsewhere link there.
    for sec in sections.values():
        for act in sec["actions"]:
            if "open" in act:
                assert act["open"].startswith("?"), (sec["key"], act)
    targets = sections["targets"]
    assert {a["label"]: a["open"] for a in targets["actions"]} == {
        "Add a target": f"?project_id={helpdesk['id']}&area=infra&add=target",
        "Add a server": f"?project_id={helpdesk['id']}&area=infra&add=host"}
    target = next(i for i in targets["items"] if i["kind"] == "target")
    assert target["href"] == f"?project_id={helpdesk['id']}&area=infra&target={target['id']}"
    assert all("view=hosts" not in i.get("href", "") for i in targets["items"])
    assert sections["repos"]["items"][0]["href"].startswith("https://github.com/")
    assert sections["notifications"]["href"].endswith("area=observability&view=notify")
    assert {a["label"] for a in sections["autonomy"]["actions"] if a["open"].endswith(
        "area=budget&tab=autonomy")} == {"Ambient budget"}


def test_a_demo_password_is_revealed_only_to_an_owner_with_a_fresh_step_up(world, client,
                                                                           helpdesk, engine):
    from boss_engine.config import get_settings
    from boss_engine.services.stack_deploy import ensure_generated_secret

    pid = helpdesk["id"]
    value = ensure_generated_secret(engine, get_settings(), project_id=pid,
                                    target_id=helpdesk["target_id"], stack="helpdesk-ai-demo",
                                    key="DEMO_PASSWORD", generator="password", actor="test")
    path = f"/v1/projects/{pid}/demo-logins/reveal"
    with world.acting_as(world.ctx(world.viewer)):
        assert client.get(path, params={"env": "staging"}).status_code == 403
    with world.acting_as(world.ctx(world.owner, mfa=False)):
        stale = client.get(path, params={"env": "staging"})
    assert stale.status_code == 401 and stale.json().get("error") == "step_up_required"
    with world.acting_as(world.ctx(world.owner)):
        shown = client.get(path, params={"env": "staging"})
        missing = client.get(path, params={"env": "production"})
    assert shown.status_code == 200, shown.text
    assert shown.headers["cache-control"] == "no-store"
    logins = shown.json()["logins"]
    assert [x["user"] for x in logins] == [u["user"] for u in DEMO_USERS]
    assert {x["password"] for x in logins} == {value}
    assert missing.status_code == 404
    with engine.connect() as db:
        audited = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
            "'project.demo_logins_revealed'"), {"p": pid}).scalar()
    assert audited == 1


def test_no_read_ever_carries_a_demo_password(world, client, helpdesk):
    for tab in ("overview", "deployments", "settings", "demo-logins"):
        body = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/{tab}").text
        assert '"password":' not in body, tab


# ── permissions and speed ───────────────────────────────────────────────────────────────────

def test_a_viewer_reads_every_tab_and_a_stranger_reads_none(world, client, helpdesk):
    for tab in READS:
        seen = _get(world, client, world.viewer, f"/v1/projects/{helpdesk['id']}/{tab}")
        assert seen.status_code == 200, (tab, seen.text)
        hidden = _get(world, client, world.stranger, f"/v1/projects/{helpdesk['id']}/{tab}")
        assert hidden.status_code in (403, 404), (tab, hidden.status_code)


def test_each_tab_answers_in_one_fast_round_trip(world, client, helpdesk):
    slow = {}
    for tab in READS + ("health",):
        _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/{tab}")  # warm
        started = time.perf_counter()
        r = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/{tab}")
        took = time.perf_counter() - started
        assert r.status_code == 200
        print(f"{tab}: {took * 1000:.0f} ms")
        if took > 1.0:
            slow[tab] = round(took, 2)
    assert not slow, slow


def test_the_old_demo_logins_door_has_the_same_bar_as_project_settings(world, client, helpdesk,
                                                                       engine):
    """Two doors to the same passwords (the build's Settings sheet, Project › Settings): both are
    owner/admin with a fresh step-up, and both are audited. The old one let an operator in."""
    path, params = "/v1/apps/helpdesk-ai-demo/demo-logins", {"project_id": helpdesk["id"],
                                                             "env": "deploy"}
    with world.acting_as(world.ctx(world.operator)):
        assert client.get(path, params=params).status_code == 403
    with world.acting_as(world.ctx(world.viewer)):
        assert client.get(path, params=params).status_code == 403
    with world.acting_as(world.ctx(world.owner, mfa=False)):
        stale = client.get(path, params=params)
    assert stale.status_code == 401 and stale.json().get("error") == "step_up_required"
    with world.acting_as(world.ctx(world.owner)):
        shown = client.get(path, params=params)
    assert shown.status_code == 200, shown.text
    assert shown.headers["cache-control"] == "no-store"
    with engine.connect() as db:
        audited = db.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
            "'project.demo_logins_revealed'"), {"p": helpdesk["id"]}).scalar()
    assert audited == 1


def test_the_projects_list_shows_each_projects_week_of_spend(world, client, helpdesk):
    r = _get(world, client, world.owner, "/v1/projects/summary")
    card = next(i for i in r.json()["items"] if i["id"] == helpdesk["id"])
    assert isinstance(card["cost_week"], (int, float)) and len(card["series"]) == 7
    overview = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/overview").json()
    assert card["cost_week"] == overview["tiles"]["cost_week"]["usd"]


def test_the_old_demo_logins_sheet_now_names_who_signs_in(world, client, helpdesk):
    """The open bug: Settings › Demo logins showed the password but not the user names."""
    with world.acting_as(world.ctx(world.owner)):
        r = client.get("/v1/apps/helpdesk-ai-demo/demo-logins",
                       params={"project_id": helpdesk["id"], "env": "deploy"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [u["user"] for u in body["users"]] == [u["user"] for u in DEMO_USERS]
    assert body["users_from"] == "manifest"
    for row in body["demo_logins"]:
        assert [u["user"] for u in row["users"]] == [u["user"] for u in DEMO_USERS]


def test_a_manifest_names_its_demo_users_and_never_a_password():
    from boss_engine.services.app_deploy_manifest import ManifestError, load_manifest

    m = load_manifest("version: 1\nname: helpdesk\nkind: commands\n"
                      "install: [{name: up, run: npm start}]\n"
                      "env:\n  DEMO_PASSWORD: {generate: password}\n"
                      "demo_logins:\n  - {user: agent@demo.test, label: Agent}\n"
                      "  - customer@demo.test\n")
    assert [dict(d) for d in m.demo_logins] == [
        {"user": "agent@demo.test", "label": "Agent", "password": "DEMO_PASSWORD"},
        {"user": "customer@demo.test", "label": "Customer", "password": "DEMO_PASSWORD"}]
    assert m.summary()["demo_logins"][0]["user"] == "agent@demo.test"
    for bad in ("demo_logins: {user: x}", "demo_logins: [{label: Agent}]",
                "demo_logins: [{user: a@b.c, pass: hunter2}]",
                "demo_logins: [{user: a@b.c, password: 'hunter2!'}]"):
        with pytest.raises(ManifestError):
            load_manifest("version: 1\nname: x\nkind: commands\n"
                          "install: [{name: up, run: npm start}]\n" + bad + "\n")


def test_the_captain_drawer_asks_about_the_whole_project(world, client, helpdesk, engine):
    from boss_engine.services.turn_subject import parse_subject, resolve_subject

    ref = parse_subject({"kind": "project", "id": helpdesk["id"]})
    assert ref is not None and ref.external_ref == f"project:{helpdesk['id']}"
    with engine.connect() as db:
        ev = resolve_subject(db, helpdesk["id"], ref)
        other = resolve_subject(db, "someone-elses-project", ref)
    assert ev.found and ev.title == "HelpDesk AI Demo"
    assert "1 running" in ev.status and "2 needs you" in ev.status
    assert ev.facts["repositories"] == REPO
    assert any("Ticket search filters" in line for line in ev.linked)
    assert not other.found
