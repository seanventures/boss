"""The Work panel reads one piece of work from the record (park-bench-map, 2026-11-19).

Run #2 "Bench Name Label on Map Bubble": change #3 opened by "Follow-up: More tests" (Retry 1)
waits on its merge while Follow-up 1 is still fixing 4 findings and the failed "Package honest"
gate on that same change; its acceptance check read 0 met · 4 partly. The wireframe showed:

  * Merge offered as the next step while a follow-up was still fixing the change;
  * "What was built" empty, because the drawer read the running follow-up's (empty) record;
  * "Ready to merge" over 0 must-haves fully met.

The API now carries what the panel draws: per-station agent / started_at / duration_s, the
merge's `build_id`, `fixing` and `acceptance`, the explanation of the build that opened the
change (with a one-sentence summary and must-haves with verdicts), and deduped review comments.
Fixtures shaped like those rows. Real Postgres.
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

from boss_engine.services.project_experience import explained, work, work_story

PRD = ("# PRD: Bench Name Label on Map Bubble\n\n## 1. Summary\n\nThe map bubble does not show the "
       "bench's name.\n\n## 3. Goals & Non-Goals\n\n**Goals**\n- Show the bench's name as the "
       "first line of the bubble.\n- Handle benches with no name gracefully.\n\n**Non-Goals**\n"
       "- No redesign of the bubble.\n")
STEPS = [("meeting", "capability", "meeting"), ("build", "capability", "build"),
         ("preview", "capability", "preview"), ("more_tests", "capability", "more_tests"),
         ("report", "capability", "report"), ("deploy", "capability", "deploy")]
URL = "http://boss-local/local_park-bench-map-e86c6d9e/park-bench-map/pull/3"
MUSTS = ["Clicking a bench marker that has a name shows that name as the first line.",
         "A bench with no name shows \"Bench #<id>\".",
         "Special characters are shown safely as text.",
         "Existing map behaviour is unchanged."]
DEFERRED = [
    {"text": "Failing gate 'web_package_honest' (Package honest) when the PR opened",
     "severity": "blocker"},
    {"text": "The nameless marker sits outside the 420px map, so three browser tests time out.",
     "path": "tests/popup-browser.test.js", "line": 34, "severity": "major"},
    {"text": "Browser launch occurs before the cleanup try/finally.",
     "path": "tests/popup-browser.test.js", "line": 54, "severity": "minor"},
] + [{"text": f"Must-have {i + 1} (P1) partial: {m}", "severity": "major"}
     for i, m in enumerate(MUSTS)]
FOLLOWUPS = {"count": 3, "items": [
    {"text": "Failing gate 'web_package_honest' (Package honest) when the PR opened",
     "tier": "P1"},
    {"text": "tests/popup-browser.test.js:34 — The nameless marker sits outside the 420px map, "
             "so three browser tests time out.", "tier": "P1"},
    {"text": "Must-have 1 (P1) partial: " + MUSTS[0], "tier": "P1"}]}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("px-panel-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client):
    slug = f"px-panel-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Park Bench Map", "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _seed(db, pid: str, *, fixing: bool = True, met: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    at = now - timedelta(minutes=70)
    did, vid, rid = _id("wfd"), _id("wfv"), _id("wfr")
    spec = {"steps": [{"id": s, "kind": k, "binding": {"capability": c}} for s, k, c in STEPS]}
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
         "in": json.dumps({"title": "The bubble present", "work_kind": "feature",
                           "directive": "The bubble on the park bench map isn't named."})})
    old_dec, merge_dec, fix_dec = _id("dec"), _id("dec"), _id("dec")
    for d, st, rec in ((old_dec, "withdrawn", {}), (merge_dec, "pending", {"followups": FOLLOWUPS}),
                       (fix_dec, "withdrawn", {})):
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
            "created_at) VALUES (:i, :p, 'Merge change #3?', CAST(:r AS JSONB), :s, 2, :t)"),
            {"i": d, "p": pid, "s": st, "t": at, "r": json.dumps(
                {"action": "review_pull_request", "pull_request_url": URL, **rec})})
    build, retry, follow = _id("crun"), _id("crun"), _id("crun")
    times = {"meeting": (0, 6), "build": (7, 11), "preview": (11, 12), "more_tests": (12, 44),
             "report": (12, 13)}
    for sid, kind, _cap in STEPS:
        out: dict = {}
        if sid == "meeting":
            out = {"prd_markdown": PRD, "meeting_run_id": _id("mrun"),
                   "ran_as": {"agent": "product_manager", "how": "picked"}}
        if sid == "build":
            out = {"await": {"id": build, "kind": "code_task"},
                   "ran_as": {"agent": "software_engineer", "how": "picked"}}
        if sid == "deploy":
            out = {"await": {"id": retry, "kind": "merge"}}
        began, ended = times.get(sid, (12, None))
        st = "waiting" if sid == "deploy" else "succeeded"
        db.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
            "status, output, decision_id, started_at, completed_at) VALUES (:i, :p, :r, :s, :k, "
            ":st, CAST(:o AS JSONB), :d, :t, :c)"),
            {"i": _id("wsr"), "p": pid, "r": rid, "s": sid, "k": kind, "st": st,
             "o": json.dumps(out), "d": merge_dec if sid == "deploy" else None,
             "t": at + timedelta(minutes=began),
             "c": at + timedelta(minutes=ended) if ended is not None else None})
    state = {"impl_files": ["public/app.js", "server.js"],
             "test_files": ["tests/popup-browser.test.js", "tests/notes-api-edge-cases.test.js"],
             "files_changed": 5, "tests_passed": True,
             "impl_summary": "Tests all pass (31/31).\n\n- `playwright` devDependency was pinned "
                             "to a Chromium build that the sandbox cache has.",
             "deferred_findings": DEFERRED}
    rows = [(build, "completed", "The bubble present", old_dec, {"workflow_run_id": rid}, {}, 7),
            (retry, "completed", "Follow-up: More tests — The bubble present", merge_dec,
             {"workflow_run_id": rid, "root_run_id": build}, state, 12),
            (follow, "running" if fixing else "completed",
             "Follow-up: deferred review findings — Follow-up: More tests — The bubble present",
             fix_dec, {"workflow_run_id": rid, "root_run_id": build, "deferred_from_run": retry},
             {}, 44)]
    for cid, status, title, dec, fctx, st, minute in rows:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
            "pull_request_number, decision_id, state, finalize_ctx, created_at, updated_at) VALUES "
            "(:i, :p, :s, :ph, :t, :u, 3, :d, CAST(:st AS JSONB), CAST(:f AS JSONB), :at, :now)"),
            {"i": cid, "p": pid, "s": status, "ph": "coding" if status == "running" else "done",
             "t": title, "u": URL, "d": dec, "st": json.dumps(st), "f": json.dumps(fctx),
             "at": at + timedelta(minutes=minute), "now": now})
    items = [{"n": i + 1, "id": f"AC-{i + 1}", "text": m,
              "status": "met" if i < met else "partial", "evidence": "public/app.js"}
             for i, m in enumerate(MUSTS)]
    db.execute(text(
        "INSERT INTO acceptance_checks (id, project_id, lineage_root_run_id, code_task_run_id, "
        "workflow_run_id, layer, mode, verdict, counts, items) VALUES (:i, :p, :root, :r, :w, "
        "'build_gate', 'static', 'not_met', CAST(:c AS JSONB), CAST(:it AS JSONB))"),
        {"i": _id("acc"), "p": pid, "root": build, "r": retry, "w": rid,
         "c": json.dumps({"met": met, "partial": 4 - met, "missing": 0, "unjudged": 0}),
         "it": json.dumps(items)})
    return {"run_id": rid, "build": build, "retry": retry, "follow": follow}


def _row(engine, pid: str, rid: str) -> dict:
    with engine.connect() as db:
        return next(w for w in work.everything(db, pid) if w["id"] == rid)


def test_each_station_says_who_did_it_and_how_long(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    stages = {s["key"]: s for s in _row(engine, project, got["run_id"])["stages"]}
    assert stages["plan"]["agent"]["role_key"] == "product_manager"
    assert stages["plan"]["duration_s"] == 6 * 60
    assert stages["build"]["agent"]["role_key"] == "software_engineer"
    # Build, preview, more tests and report are the Build station's steps: 4 + 1 + 32 + 1 min.
    assert stages["build"]["duration_s"] == (4 + 1 + 32 + 1) * 60
    assert stages["build"]["started_at"]
    # Merge is a person's: no agent face on it.
    assert stages["pr"]["agent"] is None


def test_merge_says_a_follow_up_is_still_fixing_the_same_change(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    mp = _row(engine, project, got["run_id"])["links"]["merge_pr"]
    assert mp["number"] == 3 and mp["build_id"] == got["retry"]
    assert mp["fixing"]["attempt"] == got["follow"]
    assert mp["fixing"]["label"].startswith("Follow-up")
    # Deduped: the gate once, two file findings; the four must-have lines are the meter's.
    assert mp["fixing"]["findings"] == 3
    assert mp["acceptance"] == {"met": 0, "partial": 4, "missing": 0, "total": 4}


def test_nothing_is_fixing_once_the_follow_up_finished(engine, project):
    with engine.begin() as db:
        got = _seed(db, project, fixing=False, met=4)
    mp = _row(engine, project, got["run_id"])["links"]["merge_pr"]
    assert mp["fixing"] is None
    assert mp["acceptance"]["met"] == 4


def test_what_was_built_reads_the_build_that_opened_the_change(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    with engine.connect() as db:
        story = work_story.story(db, project, got["run_id"])
    built = story["explained"]["built"]
    # Never the running follow-up (its record is empty): the build whose merge is open.
    assert built["build_id"] == got["retry"]
    assert built["summary"] == "Show the bench's name as the first line of the bubble. Tests pass."
    items = built["acceptance"]["items"]
    assert [i["verdict"] for i in items] == ["partial"] * 4
    assert items[0]["text"].startswith("Clicking a bench marker")


def test_review_comments_come_once_each(engine, project):
    with engine.begin() as db:
        got = _seed(db, project)
    with engine.connect() as db:
        story = work_story.story(db, project, got["run_id"])
    found = story["review_comments"]
    sev = [c["severity"] for c in found["items"]]
    assert sev == ["gate", "P1", "P2"]
    gate = found["items"][0]
    assert gate["source"] == "web_package_honest" and gate["merged"] == 2
    assert "Package honest" in gate["text"]
    # tests/popup-browser.test.js:34 from the merge card is the same comment as the finding.
    assert found["items"][1]["merged"] == 2 and found["items"][1]["line"] == 34
    assert found["raw"] == len(DEFERRED) + len(FOLLOWUPS["items"])


def test_main_build_and_summary_rules():
    builds = [{"id": "a", "status": "completed", "pull_request_number": 3, "finalize_ctx": {}},
              {"id": "b", "status": "completed", "pull_request_number": 3, "finalize_ctx": {}},
              {"id": "c", "status": "running", "pull_request_number": 3, "finalize_ctx": {}}]
    assert explained.main_build(builds, {"build_id": "a", "number": 3})["id"] == "a"
    assert explained.main_build(builds, {"number": 3})["id"] == "b"
    assert explained.main_build(builds)["id"] == "b"
    # Tooling noise never becomes the summary; with nothing else there is none.
    assert explained.summary_of({"highlights": ["playwright devDependency was pinned to 1.2"]}) is None
    assert explained.summary_of({"highlights": ["Bench markers now show the name."],
                                 "tests": {"passed": True}}) == \
        "Bench markers now show the name. Tests pass."
