"""Project experience, round two (BOSSAPP-913/914): what the laptop walk found, fixed as classes.

* Observability › Apps across projects (screen 15): GET /v1/observability/apps/all lists every
  app of every project the caller may read, each with its project, uptime · p95, the alerts
  firing on it and the link to that project's Health tab; a stranger's projects never appear.
* Costs said one way: work that ran on a subscription carries its list price (`list_usd`), the
  Projects card its week at list price (`list_week`, `on_subscription`) — never a bare $0.00.
* Deployments: "Last deploy" is the time that commit went LIVE, not when a later row about the
  same commit was written; the env carries a true 24 h strip of hourly bars (`hours`).
* Projects cards: Working only while work moves; a quiet project's stage is done, not pulsing.

Real Postgres and the HelpDesk seed; no model, no GitHub.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("r2-owner")
    w.bind(w.owner, "owner")
    w.stranger = w.person("r2-stranger")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _project(world, client, engine, name: str) -> dict:
    slug = f"r2-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


@pytest.fixture
def helpdesk(world, client, engine):
    return _project(world, client, engine, "HelpDesk AI Demo")


def _get(world, client, who, path):
    with world.acting_as(world.ctx(who)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_observability_lists_every_app_across_the_projects_you_may_read(world, client, engine,
                                                                       helpdesk):
    other = _project(world, client, engine, "NorthPeak Retail")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, alertname, "
            "state, active_at) VALUES (:i, :p, :t, 'threshold', :f, '5xx', 'firing', now())"),
            {"i": f"aal_{uuid.uuid4().hex[:10]}", "p": other["id"], "t": other["target_id"],
             "f": uuid.uuid4().hex})
    body = _get(world, client, world.owner, "/v1/observability/apps/all?win=24h")
    rows = {r["target_id"]: r for r in body["rows"]}
    mine, theirs = rows[helpdesk["target_id"]], rows[other["target_id"]]
    assert mine["project"] == {"id": helpdesk["id"], "name": "HelpDesk AI Demo", "initials": "HA"} \
        or mine["project"]["name"] == "HelpDesk AI Demo"
    assert theirs["project"]["name"] == "NorthPeak Retail"
    assert mine["uptime"] == 1.0 and mine["p95_ms"]
    assert len(mine["strip"]) > 0
    assert theirs["firing"] >= 1 and mine["firing"] == 0
    assert "area=project" in theirs["health_href"] and "tab=health" in theirs["health_href"]
    assert f"project_id={other['id']}" in theirs["health_href"]
    # A firing app sorts before a quiet one in the same state.
    order = [r["target_id"] for r in body["rows"]]
    assert order.index(other["target_id"]) < order.index(helpdesk["target_id"])
    # A stranger reads none of them.
    with world.acting_as(world.ctx(world.stranger)):
        seen = client.get("/v1/observability/apps/all").json()
    assert not {helpdesk["target_id"], other["target_id"]} & {r["target_id"] for r in seen["rows"]}


def test_work_on_a_subscription_says_its_list_price_never_a_bare_zero(world, client, engine,
                                                                      helpdesk):
    merged = helpdesk["merged"][0]
    with engine.begin() as db:
        db.execute(text("UPDATE llm_calls SET included_in_subscription = true "
                        "WHERE code_task_run_id = :b"), {"b": merged["build_id"]})
    items = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/work")["items"]
    row = next(w for w in items if w["id"] == merged["run_id"])
    assert row["cost_usd"] == 0.0 and row["list_usd"] == 2.1
    paid = next(w for w in items if w["id"] == helpdesk["merged"][1]["run_id"])
    assert paid["cost_usd"] == 2.1 and paid["list_usd"] is None
    with engine.begin() as db:
        db.execute(text("UPDATE llm_calls SET included_in_subscription = true, created_at = now() "
                        "WHERE project_id = :p"), {"p": helpdesk["id"]})
    cards = _get(world, client, world.owner, "/v1/projects/summary")["items"]
    card = next(c for c in cards if c["id"] == helpdesk["id"])
    # The week's list price rides with its money; "on subscription" exactly when no money moved.
    assert card["list_week"] > 0
    assert card["on_subscription"] is (card["cost_week"] == 0)


def test_last_deploy_is_when_the_commit_went_live_and_the_strip_is_hourly(world, client, engine,
                                                                         helpdesk):
    pid = helpdesk["id"]
    d0 = _get(world, client, world.owner, f"/v1/projects/{pid}/deployments")
    env = next(e for e in d0["environments"] if e["target_id"] == helpdesk["target_id"])
    live_at = env["last_deploy"]["at"]
    commit = env["last_deploy"]["commit"]
    # A later row about the same live commit (a re-observation / a backfill) is written now.
    with engine.begin() as db:
        full = db.execute(text("SELECT commit_sha, host_id FROM stack_deployments WHERE target_id = :t "
                               "AND left(commit_sha, 7) = :c AND status = 'healthy' LIMIT 1"),
                          {"t": helpdesk["target_id"], "c": commit}).mappings().one()
        db.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
            "commit_sha, manifest, status, created_by, created_at, finished_at) VALUES (:i, :p, :t, :h, "
            ":r, 'main', :c, '{}'::jsonb, 'healthy', 'boss', now(), now())"),
            {"i": f"sdep_{uuid.uuid4().hex[:10]}", "p": pid, "t": helpdesk["target_id"],
             "h": full["host_id"], "r": REPO, "c": full["commit_sha"]})
        # An hour with failed checks: amber in the strip.
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
        for k in range(4):
            db.execute(text(
                "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
                "checked_at, ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, :ok, 500, 90)"),
                {"i": f"dhs_{uuid.uuid4().hex[:10]}", "p": pid, "t": helpdesk["target_id"],
                 "at": hour + timedelta(minutes=5 + k), "ok": k == 0})
    d1 = _get(world, client, world.owner, f"/v1/projects/{pid}/deployments")
    env = next(e for e in d1["environments"] if e["target_id"] == helpdesk["target_id"])
    assert env["last_deploy"]["at"] == live_at
    hours = env["hours"]
    assert len(hours) == 24
    assert all(h["state"] in ("healthy", "degraded", "down", "none") for h in hours)
    assert [h["at"] for h in hours] == sorted(h["at"] for h in hours)
    bad = hours[-4]
    assert bad["n"] >= 4 and bad["state"] in ("degraded", "down")
    assert any(h["state"] == "healthy" for h in hours)


def test_a_quiet_project_is_idle_not_working(world, client, engine, helpdesk):
    pid = helpdesk["id"]
    with engine.begin() as db:
        db.execute(text("UPDATE workflow_runs SET status = 'completed', completed_at = now() "
                        "WHERE project_id = :p"), {"p": pid})
        db.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE project_id = :p"),
                   {"p": pid})
        db.execute(text("UPDATE decisions SET status = 'rejected' WHERE project_id = :p "
                        "AND status = 'pending'"), {"p": pid})
    card = next(c for c in _get(world, client, world.owner, "/v1/projects/summary")["items"]
                if c["id"] == pid)
    assert card["running"] == 0
    assert all(j["state"] != "now" for j in card["journey"]), card["journey"]
    assert card["status_label"] != "Working"


def test_each_overview_card_loads_or_fails_on_its_own(world, client, engine, helpdesk,
                                                      monkeypatch):
    from boss_engine.services.project_experience import overview

    def broken(*_a, **_k):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(overview, "_cost_week", broken)
    monkeypatch.setattr(overview, "week", broken)
    ov = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/overview")
    assert set(ov["errors"]) == {"tile_cost", "week"}
    assert ov["tiles"]["cost_week"] is None and ov["week"] == []
    # Every other card still answers.
    assert ov["journey"] and ov["needs_you"] and ov["recent_work"] and ov["captain"]
    assert ov["tiles"]["open_work"]["count"] > 0
    monkeypatch.undo()
    assert _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/overview")["errors"] == {}


def test_a_merge_makes_the_dossier_due_for_a_re_read(engine, helpdesk):
    """06-22: the dossier is re-read on every merge — the merge marks the repository's code
    graph due now (reason `merged`); the index job that follows re-reads the dossier."""
    from boss_engine.services import pr_merge

    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO kg_repo_index (project_id, binding_id, repo, branch, status, due, reason, "
            "indexed_sha) VALUES (:p, :b, :r, 'main', 'indexed', false, 'registered', 'old') "
            "ON CONFLICT (project_id, binding_id) DO UPDATE SET due = false, status = 'indexed'"),
            {"p": helpdesk["id"], "b": helpdesk["binding_id"], "r": REPO})
        pr_merge.record(db, helpdesk["merge"]["build_id"], "9f9f9f9f9f9f", source="github")
        row = db.execute(text("SELECT due, reason FROM kg_repo_index WHERE project_id = :p"),
                         {"p": helpdesk["id"]}).mappings().one()
    assert row["due"] is True and row["reason"] == "merged"


def test_the_access_strip_is_the_real_result_with_its_reasons(world, client, engine, helpdesk):
    """06-04: Read / Branch / Open PR are the registry's last checks (red with the reason when
    one failed); Staging is the newest deploy to staging, never "a target exists"."""
    with engine.begin() as db:
        db.execute(text(
            "UPDATE github_repository_bindings SET checks = CAST(:k AS JSONB) WHERE id = :b"),
            {"b": helpdesk["binding_id"], "k": '{"read": {"ok": true}, "write": {"ok": false, '
             '"error": "The token cannot push to main"}, "pull_requests": {"ok": true}}'})
    k = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/knowledge")
    repo = k["repos"][0]
    assert repo["access"] == {"read": True, "write": False, "pr": True}
    assert repo["access_reasons"]["write"] == "The token cannot push to main"
    assert repo["access_reasons"]["read"] is None
    assert repo["deploy_check"]["label"] == "Staging" and repo["deploy_check"]["state"] == "done"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
            "commit_sha, manifest, status, failure_reason, created_by, created_at, finished_at) "
            "VALUES (:i, :p, :t, :h, :r, 'main', 'deadbee', '{}'::jsonb, 'failed', "
            "'Port 8004 is taken on the host', 'boss', now(), now())"),
            {"i": f"sdep_{uuid.uuid4().hex[:10]}", "p": helpdesk["id"], "t": helpdesk["target_id"],
             "h": helpdesk["host_id"], "r": REPO})
    repo = _get(world, client, world.owner, f"/v1/projects/{helpdesk['id']}/knowledge")["repos"][0]
    assert repo["deploy_check"] == {"label": "Staging", "state": "failed",
                                    "reason": "Port 8004 is taken on the host"}


def test_roadmap_cards_name_their_detector_as_the_wireframe_does_with_its_icon(world, client,
                                                                              engine, helpdesk):
    """07-09 / 07-12: "Incident hotspot" (detective) and "Alert gap" (bell-slash), read by the
    alert-gap detector from the live staging target."""
    from apps.api.tests.project_experience_seed import seed_round_two

    with engine.begin() as db:
        seed_round_two(db, helpdesk["id"])
    got = _get(world, client, world.owner,
               f"/v1/ambient/roadmap?project_id={helpdesk['id']}&status=open&limit=30")
    nodes = {r["title"]: next(n for n in r["strip"] if n["type"] == "agent") for r in got["items"]
             if any(n["type"] == "agent" for n in r["strip"])}
    assert nodes["Regression test for SLA weekend pause"] == {
        "type": "agent", "label": "Incident hotspot", "badges": [], "icon": "detective"}
    alert = nodes["Alert rule for /api/tickets 5xx"]
    assert (alert["label"], alert["icon"]) == ("Alert gap", "bell-slash")
    card = next(r for r in got["items"] if r["title"] == "Alert rule for /api/tickets 5xx")
    assert card["strip"][0]["label"] == "Live app" and card["squad"] == ["sre"]
