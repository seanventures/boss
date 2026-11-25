"""BOSSAPP-1406: Easy Mode prod_waits suppression, waiting_on_person surfaces, org workflow runs,
and plain-word activity classification.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.config import get_settings
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import (
    activity_classify,
    agents_live_board,
    command_center,
    command_center_view,
)
from boss_engine.services.project_experience import cards as px_cards, needs as px_needs, work as px_work


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("em-wait-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _easy_project(world: IdentityWorld, client: TestClient, engine, *, name: str) -> tuple[str, str, str]:
    slug = f"em-wait-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    did, vid, rid = (f"wfd_{uuid.uuid4().hex[:12]}", f"wfv_{uuid.uuid4().hex[:12]}",
                     f"wfr_{uuid.uuid4().hex[:12]}")
    spec = {
        "entry_step": "meeting",
        "steps": [
            {"id": "meeting", "kind": "capability", "binding": {"capability": "meeting"}},
            {"id": "build", "kind": "capability", "binding": {"capability": "build"}},
        ],
    }
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        existing = c.execute(text(
            "SELECT d.id AS did, v.id AS vid FROM workflow_definitions d "
            "LEFT JOIN workflow_versions v ON v.workflow_definition_id = d.id AND v.status = 'published' "
            "WHERE d.project_id = :p AND d.workflow_key = 'easy_mode_mvp' "
            "ORDER BY v.version DESC NULLS LAST LIMIT 1"), {"p": pid}).mappings().first()
        if existing and existing["did"] and existing["vid"]:
            did, vid = str(existing["did"]), str(existing["vid"])
        else:
            if existing and existing["did"]:
                did = str(existing["did"])
            else:
                c.execute(text(
                    "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, namespace, "
                    "is_catalogue, created_by) VALUES (:d, :p, 'easy_mode_mvp', 'Easy Mode MVP', "
                    "'product', true, 'ceo')"), {"d": did, "p": pid})
            c.execute(text(
                "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                "status, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, 'published', "
                "CAST(:s AS JSONB), :v, 'ceo')"), {"v": vid, "p": pid, "d": did, "s": json.dumps(spec)})
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "workflow_version_id, actor, status, input, created_at, updated_at) "
            "VALUES (:r, :p, :d, :v, 'ceo', 'running', "
            "CAST('{\"title\":\"Notes MVP\"}' AS JSONB), :t, :t)"),
            {"r": rid, "p": pid, "d": did, "v": vid, "t": now - timedelta(minutes=2)})
        c.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
            "step_kind, status, output, iteration, attempt, started_at, created_at) "
            "VALUES (:sid, :p, :r, 'meeting', 'capability', 'waiting', CAST(:o AS JSONB), "
            "1, 1, :t, :t)"),
            {"sid": f"wfs_{uuid.uuid4().hex[:12]}", "p": pid, "r": rid, "t": now - timedelta(minutes=1),
             "o": json.dumps({
                 "waiting_on_person": "Your model at http://10.0.0.7:8080 is plain http; BOSS only sends keys over https.",
                 "awaited_note": "Waiting on you: change model to https.",
                 "meeting_run_id": "mrun_wait_1",
             })})
    return pid, rid, vid


def test_easy_mode_suppresses_prod_waits_and_surfaces_waiting_on_person(world, client, engine):
    pid, rid, _ = _easy_project(world, client, engine, name="Easy Mode Notes")
    with engine.begin() as c:
        # Even with a healthy staging deployment and no prod target, Easy Mode never asks Pick a prod host.
        tid = f"dt_{uuid.uuid4().hex[:10]}"
        sid = f"dep_{uuid.uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, status, created_by) VALUES (:id, :p, 'notes', 'local/notes', "
            "'staging', 'docker_compose', 'ref:staging', 'active', 'ceo')"),
            {"id": tid, "p": pid})
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            "commit_sha, status, created_by) VALUES (:id, :p, :t, 'local/notes', 'main', 'abc1234', 'healthy', 'ceo')"),
            {"id": sid, "p": pid, "t": tid})

        assert px_needs.prod_waits(c, pid) is False
        assert not any(card["id"] == "prod:host" for card in px_needs.setup_cards(c, pid))

        # Work tab: waiting_on_person marks the run needs_you and its Plan stage waiting.
        items = px_work.everything(c, pid)
        run_item = next(i for i in items if i["id"] == rid)
        assert run_item["state"] == "needs_you"
        assert run_item["action_label"] == "Fix model"
        plan_stage = next(s for s in run_item["stages"] if s["key"] == "plan")
        assert plan_stage["state"] == "waiting"

        # Project live_work: does NOT count waiting_on_person as live/Working.
        lw = px_cards.live_work(c, [pid])[pid]
        assert lw["count"] == 0 and lw["agents"] == []

        # Live board & Command Center: state is waiting_you, not running / quiet.
        board = agents_live_board.build(c, get_settings(), pid)
        mission = next(m for m in board["missions"] if m["run_id"] == rid)
        assert mission["state"] == "waiting_you"
        assert mission["verb"] == "Needs you"

        cc = command_center.build(c, get_settings(), pid)
        assert cc["running"] == []

        ccv = command_center_view.build(c, get_settings(), pid)
        assert ccv["status"]["key"] == "needs_you"
        assert ccv["status"]["text"] == "1 needs you"


def test_org_scope_delivery_workflows_runs_returns_sibling_project_runs(world, client, engine):
    home_slug = f"em-home-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        home_res = client.post("/v1/projects", json={"name": "Home Project", "slug": home_slug})
    assert home_res.status_code == 201, home_res.text
    home_id = home_res.json()["id"]
    pid, rid, _ = _easy_project(world, client, engine, name="Sibling Easy App")

    with world.acting_as(world.ctx(world.owner)):
        proj_only = client.get("/v1/delivery-workflows/runs", params={"project_id": home_id})
        org_wide = client.get("/v1/delivery-workflows/runs",
                              params={"project_id": home_id, "scope": "org"})
    assert proj_only.status_code == 200 and proj_only.json()["runs"] == []
    assert org_wide.status_code == 200
    found = next((r for r in org_wide.json()["runs"] if r["id"] == rid), None)
    assert found is not None
    assert found["project_id"] == pid
    assert found["project_name"] == "Sibling Easy App"
    assert found["waiting_on"] == "you"


def test_activity_classify_renders_plain_words():
    assert activity_classify.classify_activity(
        {"event_type": "workflow.panel_waits_on_person"})["title"] == "Panel waiting on you"
    assert activity_classify.classify_activity(
        {"action": "governed_code_task_service"})["title"] == "Build runner"
