"""Related runs are one piece of work, and Running means active (BOSSAPP-913/914, 2026-11-06).

The operator opened Project › Work on boss-platform and saw nineteen identical "Running" rows,
"Follow-up: checks failing — Follow-up: checks…", Feature, code_task, 3d. They were separate
runs: an original build, its retries, checks-failing follow-ups of follow-ups, every one left
`repairing` at phase 'done' with no job. The Work read now returns one row per root build with
its attempts inside, names each attempt, never stacks title prefixes, and calls a run that
stopped moving Stalled (with when) instead of Running — counted apart. Real Postgres.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import CHAIN_TITLE, seed

from boss_engine.services import run_liveness
from boss_engine.services.project_experience import work


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("px-chain-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine):
    slug = f"px-chain-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Chain Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


def _work(world, client, pid, **params):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(f"/v1/projects/{pid}/work", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_build_its_retries_and_follow_ups_are_one_row(world, client, project):
    chain = project["chain"]
    body = _work(world, client, project["id"])
    ids = {w["id"] for w in body["items"]}
    # One row, the root's; no attempt is a row of its own.
    assert chain["root"] in ids
    assert not ids & {chain[k] for k in ("r1", "f1", "f1r", "f2", "f2r", "f3")}
    row = next(w for w in body["items"] if w["id"] == chain["root"])
    assert row["title"] == CHAIN_TITLE and "Follow-up" not in row["label"]
    assert row["source_label"] == "Build" and row["kind"] == "feature"
    assert row["attempt_count"] == 7 and row["followups"] == 3 and row["retries"] == 3
    labels = [(a["id"], a["label"], a["kind"]) for a in row["attempts"]]
    assert labels == [
        (chain["f3"], "Follow-up 3 · checks failing", "followup"),
        (chain["f2r"], "Follow-up 2 · retry 1", "retry"),
        (chain["f2"], "Follow-up 2 · checks failing", "followup"),
        (chain["f1r"], "Follow-up 1 · retry 1", "retry"),
        (chain["f1"], "Follow-up 1 · checks failing", "followup"),
        (chain["r1"], "Retry 1", "retry"),
        (chain["root"], "Build", "build"),
    ]
    # The PR the work has, the cost of every attempt, and each attempt opens its own build.
    assert row["links"]["pr"]["number"] == 6
    assert row["cost_usd"] == 4.8
    assert next(a for a in row["attempts"] if a["id"] == chain["r1"])["pr"]["number"] == 6
    assert all(a["href"].endswith(f"drawer=build%3A{a['id']}") for a in row["attempts"])


def test_a_run_left_repairing_at_done_is_stalled_not_running(world, client, project):
    chain = project["chain"]
    body = _work(world, client, project["id"])
    row = next(w for w in body["items"] if w["id"] == chain["root"])
    assert row["state"] == "stalled" and row["stalled_since"]
    since = datetime.fromisoformat(row["stalled_since"])
    assert timedelta(days=1) < datetime.now(timezone.utc) - since < timedelta(days=3)
    states = {a["id"]: a["state"] for a in row["attempts"]}
    assert states[chain["f3"]] == "stalled"
    # Older repair rounds handed their work to the next attempt: history, not stuck.
    assert states[chain["root"]] == states[chain["f1"]] == states[chain["f2"]] == "handed_on"
    assert states[chain["r1"]] == "done"
    strip = {s["key"]: s["state"] for s in row["stages"]}
    assert strip["review"] == "failed" and strip["pr"] == "todo"
    # Counted apart from Running, and the Running filter does not list it.
    assert body["counts"]["stalled"] == 1 and body["counts"]["running"] == 1
    running = _work(world, client, project["id"], state="running")
    assert chain["root"] not in {w["id"] for w in running["items"]}
    stalled = _work(world, client, project["id"], state="stalled")
    assert [w["id"] for w in stalled["items"]] == [chain["root"]]


def test_the_same_row_moving_again_is_running(world, client, project, engine):
    """Active means an active phase with recent activity (or a live job) — not the status
    column alone."""
    chain = project["chain"]
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET updated_at = now(), phase = 'coding' "
                        "WHERE id = :i"), {"i": chain["f3"]})
    row = next(w for w in _work(world, client, project["id"])["items"]
               if w["id"] == chain["root"])
    assert row["state"] == "running" and row["stalled_since"] is None


def test_searching_finds_the_work_by_an_attempts_words(world, client, project):
    found = _work(world, client, project["id"], q="aml flagged")
    assert [w["id"] for w in found["items"]] == [project["chain"]["root"]]


def test_a_follow_up_of_a_workflow_runs_build_joins_that_runs_row(world, client, project,
                                                                  engine):
    """Follow-ups written before they carried their workflow run still belong to it: their root
    build names the run."""
    run = project["merged"][0]
    fid = f"crun_{uuid.uuid4().hex[:16]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
            "created_at, updated_at) VALUES (:i, :p, 'completed', 'done', :t, CAST(:f AS JSONB), "
            "now(), now())"),
            {"i": fid, "p": project["id"], "t": "Follow-up: checks failing — SLA timers",
             "f": '{"root_run_id": "%s", "parent_run_id": "%s", "deferred_from_run": "%s", '
                  '"is_followup": true}' % (run["build_id"], run["build_id"], run["build_id"])})
    items = _work(world, client, project["id"])["items"]
    assert fid not in {w["id"] for w in items}
    row = next(w for w in items if w["id"] == run["run_id"])
    assert [a["label"] for a in row["attempts"]] == ["Follow-up 1 · checks failing", "Build"]


# ── the pure rules ──────────────────────────────────────────────────────────────────────────

def test_titles_never_stack_prefixes():
    lead = "Follow-up: checks failing — "
    assert work.clean_title(f"{lead}{lead}{lead}KG P6a: the External agent step · retry 2") == \
        "KG P6a: the External agent step"
    assert work.clean_title("Follow-up: deferred review findings — Build path - e2e") == \
        "Build path - e2e"
    assert work.clean_title("Self-healing v1 (attempt 4)") == "Self-healing v1 (attempt 4)"


def test_stalled_since_is_the_one_rule():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    horizon = 55 * 60
    old = now - timedelta(hours=2)
    recent = now - timedelta(minutes=5)

    def rule(**kw):
        base = {"status": "repairing", "phase": "done", "last_activity": old,
                "live_job": False, "now": now, "horizon_seconds": horizon}
        return run_liveness.stalled_since(**{**base, **kw})

    assert rule() == old                                     # handed on, nothing since
    assert rule(last_activity=recent) is None                # inside the grace
    assert rule(live_job=True, last_activity=recent) is None
    assert rule(phase="coding", status="running") == old     # silent past the horizon
    assert rule(phase="coding", status="running", last_activity=recent) is None
    assert rule(status="completed") is None and rule(status="queued") is None
    assert rule(executing=True) is None


def test_the_handed_on_grace_is_the_shepherds_orphan_grace():
    from boss_engine.services.shepherd_service import ORPHAN_AFTER_MINUTES

    assert run_liveness.HANDED_OFF_GRACE_SECONDS == ORPHAN_AFTER_MINUTES * 60


# ── why they were stuck: the abandoned-run sweep ran for one project only ───────────────────

def test_the_shepherd_closes_handed_on_runs_in_every_project(world, client, project, engine):
    """The worker's autopilot sweeps its one default project; `watch_all` (no project named)
    now closes abandoned runs in every project that has them. Each handed-on repair round ends
    BLOCKED saying where its repair went, and the row stops claiming to work."""
    from boss_engine.config import get_settings
    from boss_engine.services import shepherd_service

    chain = project["chain"]
    assert project["id"] in shepherd_service.projects_with_unsettled_runs(engine)
    out = shepherd_service.watch_all(engine, get_settings(), jobs=None)
    assert out["abandoned_closed"] >= 4
    with engine.begin() as db:
        left = dict(db.execute(text(
            "SELECT id, status FROM code_task_runs WHERE id = ANY(:ids)"),
            {"ids": [chain[k] for k in ("root", "f1", "f2", "f3")]}).all())
    assert set(left.values()) == {"blocked"}, left
    row = next(w for w in _work(world, client, project["id"])["items"]
               if w["id"] == chain["root"])
    assert row["state"] == "failed" and row["stalled_since"] is None


# ── tester findings PX-16 / PX-17 ───────────────────────────────────────────────────────────

def test_the_action_is_named_for_what_it_does_and_a_reviewed_build_is_not_skipped(
        world, client, project):
    rows = {w["id"]: w for w in _work(world, client, project["id"])["items"]}
    assert rows[project["merge"]["run_id"]]["action_label"] == "Merge"
    assert rows[project["deploy"]["run_id"]]["action_label"] == "Approve deploy"
    merged = rows[project["merged"][0]["run_id"]]
    # The workflow has no review STEP; its build ran the review gates and opened PR #1.
    assert {s["key"]: s["state"] for s in merged["stages"]}["review"] == "done"
    assert merged["run_number"] >= 1
