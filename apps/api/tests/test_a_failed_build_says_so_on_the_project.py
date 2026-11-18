"""A FAILED BUILD SAYS SO ON THE PROJECT (recipe-box, 2026-11-18).

The Easy Mode build of Recipe box (crun_6883ec7cbd67401a9ce8) failed with "git add failed: fatal:
.git/index: index file open failed: Permission denied [substrate=local_subprocess]", and the
project page said nothing: the header chip read "Working", the journey "Working", the Running
tile "Nothing running", the Recent-work row ticked Build · Review · PR, Needs you listed only
"Pick a staging host" — and the Command Center and the Builds picker counted it as building.

Every surface that summarises a project's builds reads ONE answer (work.failed_build): the
latest build failed, why in words (never the raw error or its sandbox tag), Retry (the build's
own resume) and See what happened (its page). Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld

#: The real run's error, word for word.
REAL_ERROR = ("git add failed: fatal: .git/index: index file open failed: Permission denied "
              "[substrate=local_subprocess]")


# ── pure rules ─────────────────────────────────────────────────────────────────────────────

def test_a_failure_is_said_in_words_never_raw():
    from boss_engine.services.project_experience.work import failure_words

    said = failure_words(REAL_ERROR)
    assert said == ("It couldn't save its changes: git was not allowed to write to the build's "
                    "copy of the repo (permission denied).")
    trace = ("Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n"
             "RuntimeError: the preview never answered on port 8004 [substrate=docker]")
    assert failure_words(trace) == "The preview never answered on port 8004"
    assert failure_words("") == "BOSS didn't record why it stopped."
    assert "substrate" not in failure_words("boom [substrate=local_subprocess]")
    assert len(failure_words("x" * 400)) <= 160


def _item(**kw):
    base = {"id": "crun_a", "source": "code_task", "state": "failed", "status": "failed",
            "label": "Recipe box", "build_id": "crun_a", "build_status": "failed",
            "error": REAL_ERROR, "last_attempt_at": "2026-11-18T15:41:54+00:00",
            "href": "?project_id=p&area=project&tab=work&drawer=build%3Acrun_a", "links": {}}
    return {**base, **kw}


def test_only_the_latest_build_counts():
    from boss_engine.services.project_experience.work import failed_build

    got = failed_build("p", [_item()])
    assert got is not None and got["retry"]["href"] == "/v1/code-tasks/crun_a/retry"
    assert got["see_href"] == "?project_id=p&area=code_tasks&build=crun_a"
    # A newer build that is moving (or done) makes the old failure history.
    newer = _item(id="crun_b", build_id="crun_b", state="running", status="running",
                  last_attempt_at="2026-11-18T16:00:00+00:00")
    assert failed_build("p", [_item(), newer]) is None
    # Planned work and onboarding are not builds.
    assert failed_build("p", [_item(), _item(id="plan_1", source="roadmap", state="planned",
                                             last_attempt_at="2026-11-19T00:00:00+00:00")])
    # A build blocked at its checks has its own card (Waive / Retry), not this one.
    assert failed_build("p", [_item(status="blocked")]) is None


def test_the_strip_marks_the_pr_station_failed_when_it_failed_after_its_last_phase():
    from boss_engine.services.project_experience.work import _build_strip

    strip = {s["key"]: s["state"] for s in _build_strip({"phase": "done", "status": "failed"},
                                                        "failed")}
    assert strip["pr"] == "failed"


def test_an_app_whose_newest_build_failed_is_not_building():
    from boss_engine.services import console_v2

    t0 = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    runs = [{"status": "failed", "created_at": t0 + timedelta(minutes=40)},
            {"status": "completed", "created_at": t0}]
    assert console_v2._stage_of(runs) == ("BUILD", console_v2.STUCK)  # noqa: SLF001


# ── on a real project, modelled on the recipe-box run ──────────────────────────────────────

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("fb-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def recipe_box(world, client, engine):
    slug = f"recipe-box-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Recipe box", "slug": slug})
    assert made.status_code == 201, made.text
    run_id = f"crun_{uuid.uuid4().hex[:20]}"
    at = datetime.now(timezone.utc) - timedelta(minutes=12)
    with engine.begin() as db:
        # The real run: an Easy Mode build on its own (no workflow run), failed at phase 'done'
        # after ~9 minutes, its work item still 'active'.
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, error, finalize_ctx, "
            "created_at, updated_at) VALUES (:i, :p, 'failed', 'done', 'Recipe box', :e, "
            "CAST(:f AS JSONB), :at, :up)"),
            {"i": run_id, "p": slug, "e": REAL_ERROR, "f": json.dumps({}), "at": at,
             "up": at + timedelta(minutes=9, seconds=22)})
    return {"id": slug, "run": run_id}


def _get(world, client, path):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_project_whose_latest_build_failed_says_so_everywhere(world, client, recipe_box):
    pid, run = recipe_box["id"], recipe_box["run"]
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    failed = ov["project"]["build_failed"]
    # The header: red "Build failed", never "Working".
    assert ov["project"]["status_label"] == "Build failed"
    assert failed["build_id"] == run
    assert failed["reason"].startswith("It couldn't save its changes")
    assert "substrate" not in failed["reason"] and "fatal:" not in failed["reason"]
    # Retry is the build's own resume (the Code Tasks screen's route), never a restart.
    assert failed["retry"] == {"run_id": run, "href": f"/v1/code-tasks/{run}/retry",
                               "method": "POST"}
    assert failed["see_href"] == f"?project_id={pid}&area=code_tasks&build={run}"
    # The journey's Working node is red and says so.
    working = next(n for n in ov["journey"] if n["key"] == "working")
    assert working["state"] == "failed" and working["sub"] == "Build failed"
    assert failed["reason"] in working["detail"]
    # The Running tile names it (shown when nothing else moves).
    assert ov["tiles"]["running"]["failed"]["id"] == run
    # Needs you leads with it: the reason, Retry and See what happened.
    card = ov["needs_you"][0]
    assert card["kind"] == "failed" and card["id"] == f"failed:{run}"
    assert card["label"] == "Build failed: Recipe box" and card["detail"] == failed["reason"]
    assert card["retry"]["href"] == f"/v1/code-tasks/{run}/retry"
    assert card["see_href"] == failed["see_href"] and card["work_id"] == run
    # The Recent-work row is failed, and its strip never ticks a station it did not pass.
    row = next(w for w in ov["recent_work"] if w["id"] == run)
    assert row["state"] == "failed"
    assert {s["key"]: s["state"] for s in row["stages"]}["pr"] == "failed"
    # The projects list says the same, and counts the card it leads with.
    card_row = next(c for c in _get(world, client, "/v1/projects/summary")["items"]
                    if c["id"] == pid)
    assert card_row["status_label"] == "Build failed"
    assert card_row["build_failed"]["retry"]["href"] == f"/v1/code-tasks/{run}/retry"
    assert card_row["needs_you"] >= 1


def test_the_command_center_lists_the_failed_build(world, client, recipe_box, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import command_center, console_v2

    pid, run = recipe_box["id"], recipe_box["run"]
    with engine.connect() as c:
        cc = command_center.build(c, get_settings(), pid)
        apps = console_v2.company_overview(c, pid)
    item = next(n for n in cc["needs_you"] if n["kind"] == "build_failed")
    assert item["id"] == f"build:{run}:failed"
    assert item["title"] == "Build failed: Recipe box"
    assert item["detail"].startswith("It couldn't save its changes")
    assert item["build"]["run_id"] == run and item["build"]["retryable"] is True
    assert [a["key"] for a in item["answers"]] == ["retry"]
    # It stopped work: counted apart from decisions, and listed first.
    assert cc["counts"]["stopped"] >= 1 and cc["needs_you"][0]["kind"] == "build_failed"
    # The Builds picker reads it as failed (STUCK), never RUNNING.
    assert [p["status"] for p in apps["projects"]] == [console_v2.STUCK]


def test_a_retry_that_moves_clears_the_failure(world, client, recipe_box, engine):
    pid, run = recipe_box["id"], recipe_box["run"]
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
            "created_at, updated_at) VALUES (:i, :p, 'running', 'coding', 'Recipe box · retry 1', "
            "CAST(:f AS JSONB), now(), now())"),
            {"i": f"crun_{uuid.uuid4().hex[:20]}", "p": pid,
             "f": json.dumps({"root_run_id": run, "parent_run_id": run, "attempt": 2})})
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    assert ov["project"]["build_failed"] is None
    assert ov["project"]["status_label"] != "Build failed"
    assert not any(n["kind"] == "failed" for n in ov["needs_you"])
