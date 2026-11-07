"""Repo & knowledge reads like screen 06 (BOSSAPP-913 / 914, GAPS #14).

A HelpDesk-like project (project_experience_seed: its code graph, two business rules, three
lessons agents learned while building it, the dossier and the newest build's test run) is read
through GET /v1/projects/{id}/knowledge, and every element the wireframe draws has a real source:
the repo's last push / connection / deploy node, the dossier's install-test-build-run commands,
the 142 / 148 test ring, the code map, hotspots with heat, a graph snippet centred on the most
connected entity with rule / learned counts, PRDs and decisions with who and when, and the
learned-on-the-job rows with the run that taught them and how often later runs reused them.
With no dossier, "Build dossier now" names the onboarding pass. Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

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
    w.owner = w.person("kn-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-kn-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _knowledge(world, client, project_id):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(f"/v1/projects/{project_id}/knowledge")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_repo_row_says_branch_last_push_connection_and_where_it_ships(world, client, helpdesk):
    k = _knowledge(world, client, helpdesk["id"])
    repo = k["repos"][0]
    assert repo["full_name"] == REPO and repo["branch"] == "main"
    assert repo["access"] == {"read": True, "write": True, "pr": True}
    assert repo["deploys_to"] == "Staging"
    assert repo["binding_id"], "Re-check needs the binding to check"


def test_the_dossier_has_commands_tests_code_map_and_hotspots_with_heat(world, client, helpdesk):
    d = _knowledge(world, client, helpdesk["id"])["dossier"]
    assert d["by"] and d["built_ago"].endswith(" ago")
    assert set(d["commands"]) >= {"install", "test", "build", "run"}
    tests = d["tests"]
    assert (tests["passed"], tests["total"], tests["failing"]) == (142, 148, 6)
    assert tests["href"].startswith(f"?project_id={helpdesk['id']}&area=code_tasks")
    parts = d["code_map"]["parts"]
    assert [p["dir"] for p in parts] == ["server", "web", "tests", "infra"]
    assert d["code_map"]["files"] == 214
    assert [(h["path"], h["heat"]) for h in d["hotspots"]][:2] == [
        ("server/sla.mjs", 9), ("web/src/pages/TicketList.tsx", 6)]


def test_the_graph_is_centred_on_the_hottest_entity_with_rules_and_lessons(world, client,
                                                                              helpdesk):
    g = _knowledge(world, client, helpdesk["id"])["graph"]
    nodes = {n["id"]: n for n in g["nodes"]}
    hub = nodes[g["hub"]]
    # The dossier's most-changed file is in the graph: the snippet is centred on it.
    assert hub["kind"] == "file" and hub["label"] == "sla.mjs" and hub["group"] == "hub"
    groups = {n["group"] for n in g["nodes"][1:]}
    assert {"rule", "learned", "data"} <= groups
    assert all(len(n["label"]) <= 22 for n in g["nodes"])
    assert any(e["label"] for e in g["edges"]), "a rule edge carries its words"
    assert g["counts"] == {"nodes": 412, "rules": 2, "learned": 3}


def test_with_no_hot_file_the_most_connected_entity_is_the_centre(world, client, helpdesk,
                                                                   engine):
    with engine.begin() as db:
        db.execute(text("UPDATE project_dossiers SET facts = facts - 'hotspots' "
                        "WHERE project_id = :p"), {"p": helpdesk["id"]})
    g = _knowledge(world, client, helpdesk["id"])["graph"]
    hub = next(n for n in g["nodes"] if n["id"] == g["hub"])
    assert hub["label"] == "tickets", hub


def test_learned_on_the_job_names_the_lesson_the_run_and_its_reuse(world, client, helpdesk):
    rows = {r["label"]: r for r in _knowledge(world, client, helpdesk["id"])["learned"]}
    assert set(rows) == {"SLA timers drift outside UTC", "Workaround: npm start",
                         "Make npm run build pass"}
    tz = rows["SLA timers drift outside UTC"]
    assert tz["state"] == "used" and tz["reused"] == 2 and tz["sub"] == "PR #1 build · reused 2×"
    assert tz["agent"]["name"] and tz["href"].startswith(f"?project_id={helpdesk['id']}&area=knowledge")
    assert rows["Workaround: npm start"]["reused"] == 1
    assert rows["Make npm run build pass"]["state"] == "new"


def test_learned_labels_come_from_the_lessons_detail_never_its_raw_line():
    from boss_engine.services.project_experience.knowledge import learned_label

    assert learned_label("workaround", "': what worked after it failed", {"detail": {
        "problem": "`node -e ' import(\"./web/src/lib/x.mjs\")'` failed: boom"}}) == "Workaround: node -e"
    assert learned_label("workaround", "x", {"detail": {
        "problem": "`npm install --include=dev && npm run build && npm test 2>&1 | tail -150` failed"}}) \
        == "Workaround: npm test"
    assert learned_label("fix", "What made npm install --include=dev --no-audit && npm test pass",
                         {}) == "Make npm test pass"
    assert learned_label("test_failure", "npm test fails: the app has no test script: add a test",
                         {}) == "The app has no test script"


def test_test_counts_read_tap_pytest_and_jest_summaries():
    from boss_engine.services.project_experience.knowledge import parse_test_counts

    assert parse_test_counts("1..13\n# tests 13\n# pass 12\n# fail 1\n") == {"passed": 12, "total": 13}
    assert parse_test_counts("==== 140 passed, 2 failed in 3.1s ====") == {"passed": 140, "total": 142}
    assert parse_test_counts("Tests:       1 failed, 41 passed, 42 total") == {"passed": 41, "total": 42}
    assert parse_test_counts("no summary here") is None


def test_docs_and_decisions_have_short_titles_and_who(world, client, helpdesk, engine):
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO meeting_runs (id, project_id, directive, title, status, state) VALUES "
            "(:i, :p, :d, :d, 'completed', CAST(:s AS JSONB))"),
            {"i": f"mrun_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"],
             "d": "Finish the visual refresh the last change promised but did not deliver.",
             "s": '{"prd_markdown": "# PRD: Visual Refresh Completion — Ticket List, Dashboard\\n'
                  '**Repo:** acme"}'})
    k = _knowledge(world, client, helpdesk["id"])
    prd = next(d for d in k["docs"] if d["kind"] == "prd")
    assert prd["label"] == "PRD · Visual Refresh Completion"
    assert "Visual Refresh Completion" in prd["detail"] and "**" not in prd["detail"]


def test_no_dossier_offers_build_dossier_now_through_onboarding(world, client, helpdesk, engine):
    with engine.begin() as db:
        db.execute(text("DELETE FROM project_dossiers WHERE project_id = :p"), {"p": helpdesk["id"]})
    k = _knowledge(world, client, helpdesk["id"])
    assert k["dossier"] is None
    assert k["build"]["label"] == "Build dossier now"
    assert k["build"]["href"] == f"/v1/projects/{helpdesk['id']}/onboarding/start"
    assert k["build"]["method"] == "POST" and k["build"]["state"] != "running"
    assert k["empty"]["label"] == "No dossier yet"
