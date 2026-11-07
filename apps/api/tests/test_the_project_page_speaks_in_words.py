"""The Project page speaks in words a person reads at a glance (GAPS 4, 5, 7, 8, 9, 11, 12, 16, 17).

helpdesk-ai-demo, 2026-11-06: Needs you said "Deploy paramg/helpdesk-ai-demo@cfe343f1c6cd to
2.29.2.202" twice — once for a commit three releases old, once for the very commit already live;
every title was the brief's first words ("Finish the visual refresh the last"); "This week" drew
"PR #3 opened" four times; Deployments history said "awaiting_approval · by usr_…"; Try it had a
bare "Demo login" with no user name. These tests drive the same story (apps/api/tests/
project_experience_seed.py, incl. seed_ship_story) through the API on real Postgres.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed

RAW = re.compile(r"\b(usr|agt|dec|wfr|crun|sdep|deptgt|dept)_[a-z0-9]{6,}|awaiting_approval|"
                 r"workflow_run|code_task|needs_you|resolved_by_change|[0-9a-f]{12,}", re.I)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("Pat Owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"hd-words-{uuid.uuid4().hex[:6]}"
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


def _sweep(engine, project_id):
    from boss_engine.services.cleared_causes import withdraw_cleared

    with engine.begin() as c:
        return withdraw_cleared(c, project_id)


# ── the short title (titles.py): one helper, every surface ──────────────────────────────────

@pytest.mark.parametrize("brief,expected", [
    ("Finish the visual refresh the last change promised but did not deliver. The tick",
     "Finish the visual refresh"),
    ("Give HelpDesk a modern, polished UI and let agents set ticket priority. This IS ",
     "Give HelpDesk a modern, polished UI"),
    ("Add SLA timers to tickets: each priority gets a response deadline (urgent 1h, hi",
     "Add SLA timers to tickets"),
    ("Follow-up: deferred review findings — Finish the visual refresh the last change promised b",
     "Finish the visual refresh"),
    ("Ticket search filters", "Ticket search filters"),
    ("fix the login page so users can reset passwords", "Fix the login page"),
])
def test_a_title_is_a_short_name_never_the_briefs_first_words(brief, expected):
    from boss_engine.services.project_experience.titles import short_title

    got = short_title(brief)
    assert got == expected
    assert len(got.split()) <= 6 and not got.endswith(("the", "to", "and", "…"))


def test_a_stored_short_title_wins_and_a_real_title_is_kept():
    from boss_engine.services.project_experience.titles import short_title

    assert short_title("Add CSV export", "Add CSV export so finance can reconcile") == \
        "Add CSV export"
    assert short_title("x" * 3, "brief", stored="Email intake") == "Email intake"


def test_recent_work_and_the_running_tile_show_short_titles(world, client, helpdesk):
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    labels = [w["label"] for w in _get(world, client, f"/v1/projects/{helpdesk['id']}/work")["items"]]
    assert "Finish the visual refresh" in labels, labels
    labels += [w["label"] for w in o["recent_work"]]
    # Recent work is what moved (running, waiting, done) before roadmap items only planned.
    states = [w["state"] for w in o["recent_work"]]
    assert "planned" not in states or states.index("planned") >= len(
        [w for w in o["recent_work"] if w["state"] != "planned"])
    assert all(len(lbl.split()) <= 6 and not lbl.endswith("…") for lbl in labels)
    assert not any(lbl.startswith("Finish the visual refresh the last") for lbl in labels)
    cur = o["tiles"]["running"]["current"]
    assert cur["label"] == "Ticket search filters" and cur["sub"].startswith("Building · ")


# ── Needs you: words from the decision's data; superseded deploys withdrawn ────────────────

def test_needs_you_names_the_action_not_the_question(world, client, engine, helpdesk):
    _sweep(engine, helpdesk["id"])
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    by_label = {n["label"]: n for n in o["needs_you"]}
    assert by_label["Merge PR #3"]["action"]["label"] == "Review PR"
    assert [m["label"] for m in by_label["Merge PR #3"]["mini"]][-1] == "Merge"
    deploy = by_label["Deploy PR #5 to Staging"]
    assert deploy["action"]["label"] == "Approve deploy" and deploy["kind"] == "deploy"
    assert "drawer=needs" in deploy["action"]["href"]   # approves in its drawer, in place
    assert [m["state"] for m in deploy["mini"]] == ["done", "waiting", "todo"]
    host = by_label["Pick a prod host"]
    assert host["action"]["label"] == "Pick host" and "add=target" in host["action"]["href"]
    for n in o["needs_you"]:
        assert not RAW.search(n["label"]), n["label"]
        assert "@" not in n["label"] and "203.0.113" not in n["label"]


def test_a_deploy_request_overtaken_by_a_later_live_deploy_is_withdrawn(world, client, engine,
                                                                          helpdesk):
    """The seed writes PR #2's commit asked again 3 days ago, before PR #4 went live; the sweep
    (boot / tick, run by the seed as an install would) withdraws and closes it."""
    ship = helpdesk["shipping"]
    with engine.begin() as c:
        d = c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :i"),
                      {"i": ship["superseded_decision"]}).first()
        row = c.execute(text("SELECT status, failure_reason FROM stack_deployments WHERE id = :i"),
                        {"i": ship["superseded"]}).first()
        ev = c.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :i AND "
            "event_type = 'deployment.stack_superseded'"), {"i": ship["superseded"]}).scalar()
    assert d[0] == "withdrawn" and "later deploy" in d[1]
    assert row[0] == "rejected" and row[1].startswith("Superseded")
    assert ev == 1
    after = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    assert ship["superseded_decision"] not in {n["id"] for n in after["needs_you"]}
    # Idempotent: the next sweep finds nothing.
    assert ship["superseded_decision"] not in _sweep(engine, helpdesk["id"])
    dep = _get(world, client, f"/v1/projects/{helpdesk['id']}/deployments")
    assert ship["superseded"] not in {h["id"] for h in dep["history"]}
    # Infra reads the same row as Superseded (nobody said no).
    from boss_engine.services.infra_view import deploy_status

    assert deploy_status("rejected", decision_status="withdrawn")["word"] == "Superseded"


def _ask_to_ship(engine, project_id, target_id, commit, *, run_id=None, minutes_ago=1):
    sid, did = f"sdep_{uuid.uuid4().hex[:12]}", f"dec_{uuid.uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, created_at) "
            "VALUES (:i, :p, 'Deploy?', CAST(:r AS JSONB), 'pending', now())"),
            {"i": did, "p": project_id, "r": json.dumps({
                "action": "deploy_stack", "deployment_id": sid, "target_id": target_id,
                "commit": commit, **({"workflow_run_id": run_id} if run_id else {})})})
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
            "manifest, status, created_by, created_at, decision_id, workflow_run_id) VALUES (:i, :p, "
            ":t, :r, 'main', :c, '{}'::jsonb, 'awaiting_approval', 'ceo', "
            "now() - make_interval(mins => :m), :d, :w)"),
            {"i": sid, "p": project_id, "t": target_id, "r": REPO, "c": commit, "d": did,
             "w": run_id, "m": minutes_ago})
    return sid, did


def test_the_live_commit_asked_again_by_a_workflow_is_withdrawn_and_its_step_moves_on(
        engine, helpdesk):
    live = helpdesk["stack_deployment_id"]
    with engine.begin() as c:
        commit = c.execute(text("SELECT commit_sha FROM stack_deployments WHERE id = :i"),
                           {"i": live}).scalar()
    run_id = helpdesk["merged"][0]["run_id"]
    sid, did = _ask_to_ship(engine, helpdesk["id"], helpdesk["target_id"], commit, run_id=run_id)
    with engine.begin() as c:
        # The run's deploy step parks on the new request (the seed wrote the run's steps).
        step = c.execute(text(
            "UPDATE workflow_step_runs SET status = 'waiting', output = CAST(:o AS JSONB) "
            "WHERE id = (SELECT id FROM workflow_step_runs WHERE workflow_run_id = :r "
            "ORDER BY started_at DESC NULLS LAST LIMIT 1) RETURNING id"),
            {"r": run_id, "o": json.dumps({"await": {"kind": "stack_deployment", "id": sid}})}
        ).scalar()
    assert did in _sweep(engine, helpdesk["id"])
    with engine.begin() as c:
        out = c.execute(text("SELECT output FROM workflow_step_runs WHERE id = :i"),
                        {"i": step}).scalar()
    out = out if isinstance(out, dict) else json.loads(out)
    assert out["await"] == {"kind": "stack_deployment", "id": live}
    assert out["superseded_deployment"] == sid


def test_a_persons_own_redeploy_of_the_live_commit_stays(engine, helpdesk):
    with engine.begin() as c:
        commit = c.execute(text("SELECT commit_sha FROM stack_deployments WHERE id = :i"),
                           {"i": helpdesk["stack_deployment_id"]}).scalar()
    _sid, did = _ask_to_ship(engine, helpdesk["id"], helpdesk["target_id"], commit)
    assert did not in _sweep(engine, helpdesk["id"])


def test_going_live_withdraws_the_requests_it_overtook(engine, helpdesk):
    from boss_engine.services.cleared_causes import on_deploy_healthy

    sid, did = _ask_to_ship(engine, helpdesk["id"], helpdesk["target_id"], "a" * 40,
                            run_id=helpdesk["merged"][0]["run_id"], minutes_ago=5)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
            "manifest, status, created_by, created_at) VALUES (:i, :p, :t, :r, 'main', :c, "
            "'{}'::jsonb, 'healthy', 'ceo', now())"),
            {"i": f"sdep_{uuid.uuid4().hex[:12]}", "p": helpdesk["id"], "t": helpdesk["target_id"],
             "r": REPO, "c": "b" * 40})
        assert did in on_deploy_healthy(c, helpdesk["id"])


def test_the_card_the_page_and_the_bell_count_the_same_needs_you(world, client, engine, helpdesk):
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    card = next(i for i in _get(world, client, "/v1/projects/summary")["items"]
                if i["id"] == helpdesk["id"])
    with world.acting_as(world.ctx(world.owner)):
        live = client.get("/v1/agents/live", params={"project_id": helpdesk["id"]}).json()
    assert card["needs_you"] == len(o["needs_you"]) == live["attention"]["count"]


# ── This week, Journey, What's next ─────────────────────────────────────────────────────────

def test_this_week_is_one_point_per_thing_oldest_first(world, client, engine, helpdesk):
    with engine.begin() as c:
        # Every retry and follow-up of PR #3 carries the PR: still ONE event.
        for _ in range(3):
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
                "pull_request_number, created_at, updated_at) VALUES (:i, :p, 'completed', 'done', "
                "'retry', :u, 3, now(), now())"),
                {"i": f"crun_{uuid.uuid4().hex[:12]}", "p": helpdesk["id"],
                 "u": f"https://github.com/{REPO}/pull/3"})
    week = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")["week"]
    labels = [w["label"] for w in week]
    assert len(week) <= 6
    assert sum(1 for lbl in labels if lbl.startswith("PR #3")) == 1, labels
    assert [w["at"] for w in week] == sorted(w["at"] for w in week)
    assert all(len(lbl.split()) <= 4 for lbl in labels)


def test_the_journey_says_what_each_stage_holds(world, client, helpdesk):
    j = {n["key"]: n for n in _get(world, client,
                                   f"/v1/projects/{helpdesk['id']}/overview")["journey"]}
    assert j["working"]["state"] == "now" and j["working"]["sub"] == "3 merged"
    assert "merged" in j["working"]["detail"]
    assert j["staging"]["sub"].endswith("ago")
    assert j["production"]["state"] == "waiting" and j["production"]["sub"] == "needs you"
    assert all(n["detail"] for n in j.values())
    assert not any(re.fullmatch(r"\d+", n["sub"] or "") for n in j.values())


def test_whats_next_is_the_top_three_each_with_accept(world, client, helpdesk):
    nxt = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")["next"]
    assert 1 <= len(nxt) <= 3
    for n in nxt:
        assert n.get("accepted") or n["accept"]["href"].startswith(("/v1/ambient/roadmap/",
                                                                 f"/v1/projects/{helpdesk['id']}"))
        assert not n["label"].endswith("…")


# ── Deployments: one number, history in words ───────────────────────────────────────────────

def test_deployments_and_overview_show_the_same_p95(world, client, helpdesk):
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    d = _get(world, client, f"/v1/projects/{helpdesk['id']}/deployments")
    h = _get(world, client, f"/v1/projects/{helpdesk['id']}/health")
    staging = next(e for e in d["environments"] if e["key"] == "staging")
    hs = next(e for e in h["environments"] if e["key"] == "staging")
    assert staging["p95_ms"] == o["tiles"]["staging"]["p95_ms"] == hs["p95_ms"]
    assert staging["uptime_pct"] == hs["uptime_pct"]


def test_deploy_history_reads_in_words_with_people_by_name(world, client, engine, helpdesk):
    _sweep(engine, helpdesk["id"])
    d = _get(world, client, f"/v1/projects/{helpdesk['id']}/deployments")
    labels = [h["label"] for h in d["history"]]
    assert not any(lbl.endswith((",", " a", " the", " to")) for lbl in labels)  # never a cut phrase
    assert any(lbl.startswith("Staging · PR #4") for lbl in labels), labels
    first = next(h for h in d["history"] if h["label"] == "Staging · first deploy")
    assert first["recovered"] and "8004" in first["reason"]
    live = next(h for h in d["history"] if h["label"].startswith("Staging · PR #1"))
    assert live["duration"] == "2m 50s" and live["smoke"] == "smoke ✓"
    for h in d["history"]:
        assert not RAW.search(" ".join(str(h.get(k) or "") for k in
                                       ("label", "state_word", "by", "reason", "duration")))
    env = d["environments"][0]
    assert env["last_deploy"]["pr"] == 4 and env["host_name"]


def test_a_server_named_by_its_address_is_titled_by_its_role():
    from boss_engine.services.project_experience.deployments import host_label

    assert host_label("203.0.113.9", "203.0.113.9", "staging") == "Staging server"
    assert host_label("demo-host", "203.0.113.9", "staging") == "demo-host"


# ── Demo logins: the users the app names, found at deploy time ──────────────────────────────

def test_demo_users_are_read_from_the_repository_when_the_manifest_names_none(engine, helpdesk):
    from boss_engine.services.project_experience import demo_logins

    seed_file = 'export const DEMO_USERS = [{ email: "admin@demo.test" }, { email: "agent@demo.test" }];'
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path, repo) VALUES "
            "(:i, :p, 'file', :k, 'seedDemoUsers.mjs', 'server/seedDemoUsers.mjs', :r), "
            "(:j, :p, 'file', :k2, 'sla.mjs', 'server/sla.mjs', :r)"),
            {"i": f"kgn_{uuid.uuid4().hex[:10]}", "j": f"kgn_{uuid.uuid4().hex[:10]}",
             "p": helpdesk["id"], "k": f"file:{uuid.uuid4().hex}", "k2": f"file:{uuid.uuid4().hex}",
             "r": REPO})
        asked: list[str] = []

        def read(path):
            asked.append(path)
            return seed_file if path.endswith("seedDemoUsers.mjs") else None

        found = demo_logins.discover_in_repository(c, helpdesk["id"], REPO, read)
    assert "server/seedDemoUsers.mjs" in asked and "server/sla.mjs" not in asked
    assert [u["user"] for u in found] == ["admin@demo.test", "agent@demo.test"]
    assert [u["label"] for u in found] == ["Admin", "Agent"]


def test_a_deploy_that_found_users_names_them_everywhere(world, client, engine, helpdesk):
    with engine.begin() as c:
        c.execute(text(
            "UPDATE stack_deployments SET manifest = CAST(:m AS JSONB) WHERE project_id = :t"),
            {"t": helpdesk["id"], "m": json.dumps({
                "name": "helpdesk", "kind": "compose", "generated_env_keys": ["DEMO_PASSWORD"],
                "demo_logins_from": "repository",
                "demo_logins": [{"user": "admin@demo.test", "label": "Admin",
                                 "password_key": "DEMO_PASSWORD"}]})})
        c.execute(text(
            "INSERT INTO app_secret_bindings (id, project_id, app_key, env, name, mode, vault_item_id) "
            "SELECT :i, :p, 'helpdesk', 'deploy', 'DEMO_PASSWORD', 'generated', 'vlt_test' "
            "WHERE NOT EXISTS (SELECT 1 FROM app_secret_bindings WHERE project_id = :p "
            "AND name = 'DEMO_PASSWORD')"), {"p": helpdesk["id"], "i": f"asb_{uuid.uuid4().hex[:12]}"})
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    assert o["try_it"]["users_from"] == "repository"
    assert [d_["user"] for d_ in o["try_it"]["demo_logins"]] == ["admin@demo.test"]


# ── Projects list: no false "Onboarding" claim ──────────────────────────────────────────────

def test_a_project_never_onboarded_says_so_with_a_start(world, client, engine):
    slug = f"quiet-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        assert client.post("/v1/projects", json={"name": "Quiet One", "slug": slug}).status_code == 201
    with engine.begin() as c:   # a project made before onboarding existed: no run at all
        c.execute(text("DELETE FROM project_onboarding_runs WHERE project_id = :p"), {"p": slug})
    items = _get(world, client, "/v1/projects/summary")["items"]
    card = next(i for i in items if i["id"] == slug)
    assert card["onboarding_active"] is False
    assert card["status_label"] == "Not onboarded"
    assert card["start_onboarding"]["href"] == f"/v1/projects/{slug}/onboarding/start"


def test_an_app_deployed_before_deploys_read_users_is_named_once(world, client, engine, helpdesk):
    """The one-time repair (helpdesk-ai-demo on the laptop: source none, users [])."""
    from boss_engine.services.project_experience import demo_logins

    with engine.begin() as c:
        c.execute(text("UPDATE stack_deployments SET manifest = CAST(:m AS JSONB) "
                       "WHERE project_id = :p"),
                  {"p": helpdesk["id"], "m": json.dumps({
                      "name": "helpdesk", "kind": "compose", "generated_env_keys": ["DEMO_PASSWORD"]})})
        c.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path, repo) VALUES "
            "(:i, :p, 'file', :k, 'seedDemoUsers.mjs', 'server/seedDemoUsers.mjs', :r)"),
            {"i": f"kgn_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"], "k": f"file:{uuid.uuid4().hex}",
             "r": REPO})
    seeds = '{ email: "admin@demo.test" }, { email: "agent@demo.test" }'
    reads: list[str] = []

    def read_for(_c, _p, _owner, _repo):
        def read(path):
            reads.append(path)
            return seeds if path.endswith("seedDemoUsers.mjs") else None
        return read

    named = demo_logins.backfill_deployed_users(engine, None, read_for=read_for,
                                                project_id=helpdesk["id"])
    assert helpdesk["stack_deployment_id"] in named
    with engine.begin() as c:
        people, source = demo_logins.users(c, helpdesk["id"])
    assert source == "repository" and [p["user"] for p in people] == ["admin@demo.test",
                                                                       "agent@demo.test"]
    # Idempotent: a deploy that has names is not read again.
    reads.clear()
    assert helpdesk["stack_deployment_id"] not in demo_logins.backfill_deployed_users(
        engine, None, read_for=read_for, project_id=helpdesk["id"])
