"""A Work row says the real next step (park-bench-map, 2026-11-19).

The row "The bubble present · 2 attempts · Feature · Waiting 18m" showed **Approve deploy**, and
the drawer it opened said "Ready to merge · Merge change #3", with "Build: in progress" under a
finished build and "Staging ! Auto after merge". The run's deploy step was parked on the
build's own merge decision ("Waiting for the merge: approve Change #3 to merge it"), so its gate
read open; a later More tests pass in the Build station was still moving. And the work was
titled from the person's first words.

Fixtures shaped like that run: Minor Feature (meeting · build · preview · pass_2 [more_tests,
report] · deploy …), the build completed with change #3 waiting on its merge decision, the
deploy step waiting on that same decision, a More tests attempt running, a running preview of
change #3, and a PRD headed "# PRD: Bench Name Label on Map Bubble". Real Postgres.
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

from boss_engine.services.project_experience import work
from boss_engine.services.project_experience.titles import prd_title, short_title

BRIEF = ("The bubble present in the park bench map is not named with the bench, so it is hard "
         "to udnerstand can you please add the label please ? this must be simple code change")
PRD = "# PRD: Bench Name Label on Map Bubble\n\n**Workflow:** BOSS Minor Feature\n"
STEPS = [("meeting", "capability", "meeting"), ("build", "capability", "build"),
         ("preview", "capability", "preview"), ("pass_2", "parallel", ""),
         ("more_tests", "capability", "more_tests"), ("report", "capability", "report"),
         ("deploy", "capability", "deploy"), ("notify", "capability", "notify"),
         ("pass_2_joined", "join", ""), ("monitoring", "capability", "monitoring_pack"),
         ("deploy_again", "capability", "deploy")]
LOCAL_URL = "http://boss-local/local_park-bench-map-e86c6d9e/park-bench-map/pull/3"


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("px-next-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _seed(db, pid: str, *, with_prd: bool = True, preview_status: str = "running",
          card_checks: dict | None = None, run_checks: dict | None = None,
          more_status: str = "running") -> dict:
    now = datetime.now(timezone.utc)
    at = now - timedelta(minutes=40)
    did, vid, rid = _id("wfd"), _id("wfv"), _id("wfr")
    spec = {"steps": [{"id": s, "kind": k, **({"binding": {"capability": c}} if c else {})}
                      for s, k, c in STEPS]}
    db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                    "namespace, created_by) VALUES (:d, :p, :k, 'Minor Feature', 'product', 'ceo')"),
               {"d": did, "p": pid, "k": f"minor_feature_{uuid.uuid4().hex[:6]}"})
    db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                    "version, status, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, "
                    "'published', CAST(:s AS JSONB), :v, 'ceo')"),
               {"v": vid, "p": pid, "d": did, "s": json.dumps(spec)})
    db.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        "actor, status, input, created_at, updated_at) VALUES (:i, :p, :d, :v, 'ceo', 'waiting', "
        "CAST(:in AS JSONB), :t, :t)"),
        {"i": rid, "p": pid, "d": did, "v": vid, "t": at,
         "in": json.dumps({"title": "The bubble present", "directive": BRIEF,
                           "work_kind": "feature"})})
    merge_dec = _id("dec")
    db.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
        "created_at) VALUES (:i, :p, 'Merge change #3?', CAST(:r AS JSONB), 'pending', 2, :t)"),
        {"i": merge_dec, "p": pid, "t": at + timedelta(minutes=10),
         "r": json.dumps({"action": "review_pull_request", "pull_request_url": LOCAL_URL,
                          **({"checks_failing": card_checks} if card_checks else {})})})
    build, more = _id("crun"), _id("crun")
    status = {"meeting": "succeeded", "build": "succeeded", "preview": "succeeded",
              "pass_2": "succeeded", "report": "succeeded", "more_tests": "waiting",
              "deploy": "waiting"}
    for sid, kind, _cap in STEPS:
        out: dict = {}
        if sid == "meeting" and with_prd:
            out = {"prd_markdown": PRD, "meeting_run_id": _id("mrun")}
        if sid == "build":
            out = {"await": {"id": build, "kind": "code_task"}}
        if sid == "more_tests":
            out = {"await": {"id": more, "kind": "code_task"}}
        if sid == "deploy":
            out = {"note": "Waiting for the merge: approve Change #3 to merge it. Nothing is "
                           "deployed until it is merged.",
                   "await": {"id": build, "kind": "merge"}}
        st = status.get(sid, "pending")
        db.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
            "status, output, decision_id, started_at, completed_at) VALUES (:i, :p, :r, :s, :k, "
            ":st, CAST(:o AS JSONB), :d, :t, :c)"),
            {"i": _id("wsr"), "p": pid, "r": rid, "s": sid, "k": kind, "st": st,
             "o": json.dumps(out), "d": merge_dec if sid == "deploy" else None, "t": at,
             "c": at + timedelta(minutes=5) if st == "succeeded" else None})
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
        "pull_request_number, decision_id, finalize_ctx, created_at, updated_at) VALUES "
        "(:i, :p, 'completed', 'done', 'The bubble present', :u, 3, :d, CAST(:f AS JSONB), :at, :at)"),
        {"i": build, "p": pid, "u": LOCAL_URL, "d": merge_dec, "at": at + timedelta(minutes=2),
         "f": json.dumps({"workflow_run_id": rid, "completed_stages": ["write_tests"],
                          **({"checks_failing": run_checks} if run_checks else {})})})
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
        "created_at, updated_at) VALUES (:i, :p, :st, 'write_tests', 'More tests', "
        "CAST(:f AS JSONB), :at, :now)"),
        {"i": more, "p": pid, "st": more_status, "at": at + timedelta(minutes=6), "now": now,
         "f": json.dumps({"workflow_run_id": rid, "root_run_id": build, "attempt": "2"})})
    prev = _id("prev")
    db.execute(text(
        "INSERT INTO app_previews (id, project_id, run_id, status, mode, created_at) "
        "VALUES (:i, :p, :r, :s, 'process', :t)"),
        {"i": prev, "p": pid, "r": build, "s": preview_status, "t": at + timedelta(minutes=8)})
    return {"run_id": rid, "build": build, "more": more, "merge_dec": merge_dec, "preview": prev}


@pytest.fixture
def project(world, client):
    slug = f"px-next-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Park Bench Map", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _row(engine, pid: str, rid: str) -> dict:
    with engine.connect() as db:
        rows = work.everything(db, pid)
    return next(w for w in rows if w["id"] == rid)


def test_a_deploy_that_waits_for_the_merge_is_the_merge(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    row = _row(engine, project, got["run_id"])
    assert row["state"] == "needs_you"
    # The one action is the merge — never "Approve deploy" for a deploy that runs after it.
    assert row["action_label"] == "Merge"
    assert row["links"]["merge_pr"]["number"] == 3
    assert got["merge_dec"] in row["decision_ids"]
    stations = {s["key"]: s["state"] for s in row["stages"]}
    # Build is done (the build completed change #3); the later More tests pass does not
    # un-finish it. PR waits on the person; Ship is still to come, not waiting on anyone.
    assert stations["build"] == "done"
    assert stations["pr"] == "waiting"
    assert stations["ship"] == "todo"


def test_the_work_is_named_from_its_prd(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    row = _row(engine, project, got["run_id"])
    assert row["label"] == "Bench Name Label on Map Bubble"
    assert len(row["label"].split()) <= 6


def test_without_a_prd_the_name_is_never_a_stored_first_words_cut_longer_than_six(engine, project):
    with engine.begin() as db:
        got = _seed(db, project, with_prd=False)
    row = _row(engine, project, got["run_id"])
    assert row["label"] and len(row["label"].split()) <= 6


def test_the_row_carries_the_changes_own_preview(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    p = _row(engine, project, got["run_id"])["preview_app"]
    assert p["run_id"] == got["build"] and p["preview_id"] == got["preview"]
    assert p["state"] == "running" and p["recreate"] is False and p["href"]


def test_a_stopped_preview_is_offered_again(engine, project):
    with engine.begin() as db:
        got = _seed(db, project, preview_status="stopped")
    p = _row(engine, project, got["run_id"])["preview_app"]
    assert p["state"] == "stopped" and p["recreate"] is True and not p["href"]


def test_prd_title_reads_the_heading():
    assert prd_title(PRD) == "Bench Name Label on Map Bubble"
    assert prd_title("# PRD\n\nbody") == ""
    assert prd_title("no heading") == ""
    long = prd_title("# PRD: Add SLA timers to tickets so each priority gets its own clock")
    assert long and len(long.split()) <= 6
    # A stored name wins over the brief's first words.
    assert short_title("The bubble present", BRIEF, stored=prd_title(PRD)) == \
        "Bench Name Label on Map Bubble"



def test_a_workflow_build_is_named_from_its_prd():
    """The build page shows the same name: the build step titles its code task from the PRD."""
    from boss_engine.services.workflow_capability_runner import build_named_from_prd

    ctx = {"input": {"title": "The bubble present"}, "steps": {"meeting": {"prd_markdown": PRD}}}
    assert build_named_from_prd(ctx, {"title": "The bubble present"})["title"] == \
        "Bench Name Label on Map Bubble"
    assert build_named_from_prd(ctx, {})["title"] == "Bench Name Label on Map Bubble"
    # A step that names its build keeps it; no PRD, nothing changes.
    assert build_named_from_prd(ctx, {"title": "Fix the login"})["title"] == "Fix the login"
    assert build_named_from_prd({"input": {"title": "X"}, "steps": {}}, {"title": "X"}) == {"title": "X"}


#: A merge card's checks-failing record (checks_failing.collect), as the governed build writes it.
CHECKS = {"gates": [{"gate": "tests_pass", "label": "Tests pass", "security": False},
                    {"gate": "security_clean", "label": "Security clean", "security": True}],
          "findings": [], "security": True}


def test_a_change_opened_with_failing_checks_is_never_ready(engine, project):
    """Contributed from an enterprise install (2026-11-20): the merge card said CHECKS FAILING,
    the Work row said "Ready to merge" with Review green. The row reads the card's own record."""
    with engine.begin() as db:
        got = _seed(db, project, card_checks=CHECKS, more_status="completed")
    row = _row(engine, project, got["run_id"])
    mp = row["links"]["merge_pr"]
    assert mp["checks_failing"] == {"gates": ["security_clean", "tests_pass"], "security": True,
                                    "words": "“Security clean”, “Tests pass”"}
    # Merge stays the person's action; Review & tests is red, not a tick.
    assert row["action_label"] == "Merge"
    assert {s["key"]: s["state"] for s in row["stages"]}["review"] == "failed"


def test_the_runs_own_record_counts_when_the_card_has_none(engine, project):
    with engine.begin() as db:
        got = _seed(db, project, run_checks=CHECKS, more_status="completed")
    mp = _row(engine, project, got["run_id"])["links"]["merge_pr"]
    assert mp["checks_failing"]["gates"] == ["security_clean", "tests_pass"]


def test_passing_checks_say_nothing_about_failing(engine, project):
    with engine.begin() as db:
        got = _seed(db, project, more_status="completed")
    row = _row(engine, project, got["run_id"])
    assert row["links"]["merge_pr"]["checks_failing"] is None
    assert {s["key"]: s["state"] for s in row["stages"]}["review"] != "failed"
