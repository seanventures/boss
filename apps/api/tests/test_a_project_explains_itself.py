"""A PROJECT EXPLAINS ITSELF (2026-11-19).

The operator: "in the Overview page of the project we need to display the PRD overview and the link
to the PRD — if I want to know what this project is and what it built, today I need to navigate to
lots of places to find the PRD and its description. Also per work details should display what was
discussed and what was implemented by the Agent — we don't need to display full chat but an
explanation about what was implemented."

The fixtures are the Park bench map project's own rows (park-bench-map-e86c6d9e, trimmed): its
Easy Mode panel's PRD and synthesis, the first build (change #1, merged) with its files, tests,
review rounds and deferred findings, the acceptance check that judged it, and the later spec for
the bench name labels a person approved.

* The Overview's ``about`` card: the founding PRD's summary, its six acceptance criteria with the
  latest check's met / partial, "Approved by your Easy Mode start", the link to the spec (its
  meeting), the later spec. No PRD: the project's brief, said to be the brief.
* The build page (``GET …/builds/{id}/explained``) and the work drawer (``…/story``): the panel's
  decisions and the build's 3–6 bullets — files by purpose, tests, review, follow-ups, the merge.
* Stored once: the explanation lands on the build's finalize_ctx when it is first worked out for a
  completed build (and by ``store_build`` at completion); a later read never works it out again.
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

PRD_1 = """# PRD: Park Bench Map

**Status:** Ready for build — pending one human decision (see §10)
**Repo:** `park-bench-map` (existing repo; read before writing code)
**Date:** 2026-11-19

Stack: park-bench-map=Node/Express

---

## 1. Summary

Park Bench Map is a small single-service web app that shows at least eight named bench locations around one invented park on a MapLibre GL map, rendered entirely from assets bundled in the repo (no tile account, no API key, no network calls). Tapping a bench opens a panel with its name and a note the visitor can read and edit.

## 2. Problem & Context

Someone wants a working first version of a park-bench note-taking map today.

## 6. Proposed Solution

One Express service serves both the built static frontend (map, panel, accessible list) and a small JSON API backing the sqlite notes store.

## 8. Acceptance Criteria

1. Opening the app shows a MapLibre map rendered from the bundled style document with at least 8 bench points plotted around the invented park, with no tile account or API key used.
2. Tapping any bench point opens a panel showing that bench's name and its note (starting as placeholder text if no note exists yet).
3. Typing a note and pressing Save shows a "Saved" confirmation, and reloading the page still shows that same note for that bench.
4. Disabling the network and reloading the page still shows all bench points on the map, unchanged.
5. Selecting a bench from the plain HTML list (not the map) opens the same note panel for that bench.
6. Attempting to save a note under a failure condition shows an explicit inline error rather than failing silently.

## 9. Rollout

- Build directly into the existing `park-bench-map` repo.
"""

PRD_2 = """# PRD: Bench Name Label on Map Bubble

## 1. Summary

The map bubble (popup) that appears when a user interacts with a bench marker does not show the bench's name.

## 9. Acceptance Criteria

1. Clicking/hovering a bench marker that has a name shows that name as the first line of the bubble.
2. Clicking/hovering a bench marker with no name shows `"Bench #<id>"` as the first line.
3. All pre-existing bubble/map behavior continues to work exactly as before this change.
"""

SYNTHESIS_1 = """# Park Bench Map — Converged MVP Output

## 1. Agreed Problem & Goals

A person opens Park Bench Map today, sees named bench points, taps a bench, reads/writes a note.

## 3. Explicit Decisions & Recorded Dissent

| Decision | Resolution |
|---|---|
| **Stack line conflict** (Juno said Node/Vite, Forge said Node/Express) | **Resolved:** one service. `Stack: park-bench-map=Node/Express` — Express serves the API and the built static frontend. |
| **node:sqlite vs fallback** | Decided: try `node:sqlite` first; if Node version <22.5, fall back to `sql.js`. |
| **"Offline" scope** | Decided: strict zero-network-calls, matching Juno's and Forge's stricter read. No dissent. |
| **Service worker for offline** | Decided: **not** building one. Bundling assets locally already satisfies the offline requirement. |

---

## 4. Open Questions for the Human (BOSS)

1. **Repo state unknown:** Forge has not yet read the existing `park-bench-map` repo.
2. **Intended audience beyond this demo:** is this purely a one-off local MVP?
"""

TRANSCRIPT_LINE = "Juno: I think we should consider Vite as well, it is the faster dev loop"

IMPL_FILES = ["Dockerfile", "boss.deploy.yaml", "boss.preview.command", "data/benches.geojson",
              "docker-compose.yml", "index.js", "metrics.js", "package-lock.json", "package.json",
              "public/app.css", "public/app.js", "public/style.json", "server.js"]
TEST_FILES = ["tests/deploy-manifest.test.js", "tests/metrics.test.js", "tests/notes-api.test.js",
              "tests/offline-bundle.test.js"]
BUILD_1_STATE = {
    "impl_files": IMPL_FILES, "test_files": TEST_FILES, "files_changed": 17, "tests_passed": True,
    "impl_summary": (
        "All 15 QA-authored tests pass (`npm test`). Summary of implementation:\n\n"
        "- **Fixture & style**: `data/benches.geojson` (9 named benches around invented "
        "\"Willowmere Park\"), `public/style.json` (background-only MapLibre style).\n"
        "- **Server** (`server.js`): `createApp(dbPath)` export, Express app using `node:sqlite` "
        "for notes keyed by bench id.\n"),
    "deferred_findings": [
        {"path": "server.js", "line": 96, "severity": "major",
         "text": "`benches_viewed_total{result=\"success\"}` increments when HTML is requested, "
                 "before the GeoJSON, style, or markers load."},
        {"severity": "major", "text": "Must-have 4 (P1) partial: Disabling the network and "
                                      "reloading the page still shows all bench points."},
    ],
}
BUILD_1_FCTX = {
    "review_rounds": [
        {"round": 0, "seats": {"review_code": {"verdict": "refuted", "summary": (
            "REVIEWED: public/app.js, server.js\n\n"
            "FINDING: BLOCKER public/app.js:11 — Saved note text is inserted into `innerHTML` "
            "without escaping.\nFINDING: MAJOR public/app.js:40 — Map markers are 20×20px.\n"
            "FINDING: MINOR public/app.css:3 — The 280px map minimum overflows a 320px viewport.")}}},
        {"round": 1, "seats": {"review_code": {"verdict": "refuted", "summary": (
            "CONFIRMED FIXED: Note text is assigned through `.value`; markers are 44×44px.")}}},
    ],
    "merge": {"at": "2026-11-19T07:50:08+00:00", "sha": "97256c86d9eb392562b3129cd14236d9bad6a931",
              "source": "boss"},
}
CHECK_ITEMS = [
    {"n": i + 1, "id": f"AC-{i + 1}", "text": t, "status": s, "evidence": "public/app.js:init …"}
    for i, (t, s) in enumerate(zip(
        [ln.split(". ", 1)[1] for ln in PRD_1.split("## 8. Acceptance Criteria")[1].split("## 9")[0]
         .strip().splitlines()],
        ["partial", "met", "met", "partial", "met", "met"]))
]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("bench-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _project(world, client, engine, description: str) -> str:
    slug = f"park-bench-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Park bench map", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        db.execute(text("UPDATE projects SET description = :d WHERE id = :p"), {"d": description, "p": slug})
    return slug


def _prd(db, pid: str, markdown: str, *, at: datetime, by: str, note: str, synthesis: str = ""
         ) -> tuple[str, str]:
    """An approved meeting PRD (the panel's publish_document decision) and its completed panel."""
    dec, run = _id("dec"), _id("mrun")
    title = markdown.splitlines()[0].lstrip("# ")
    db.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
        "approved_at, resolution_note, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'approved', "
        ":by, :at, :note, :at)"),
        {"i": dec, "p": pid, "q": f"Approve {title}?", "by": by, "at": at, "note": note,
         "r": json.dumps({"tool": "publish_document", "start_code_task": True,
                          "args": {"title": "PRD: Build a web app named Park Bench Map that draws…",
                                   "markdown": markdown, "repo_subdir": "docs/prd"}})})
    db.execute(text(
        "INSERT INTO meeting_runs (id, project_id, status, phase, directive, title, role_keys, state, "
        "decision_id, created_at, updated_at) VALUES (:i, :p, 'completed', 'done', :d, :t, "
        "CAST(:rk AS JSONB), CAST(:s AS JSONB), :dec, :at, :at)"),
        {"i": run, "p": pid, "d": title, "t": title, "dec": dec, "at": at,
         "rk": json.dumps(["product_manager", "software_engineer", "ux_designer", "sre", "qa"]),
         "s": json.dumps({"synthesis": synthesis, "prd_markdown": markdown,
                          "round1": {"product_manager": TRANSCRIPT_LINE}})})
    return dec, run


def _build(db, pid: str, decision_id: str, *, at: datetime, status: str = "completed",
           state: dict | None = None, fctx: dict | None = None, number: int | None = 1) -> str:
    rid = _id("crun")
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
        "pull_request_number, state, finalize_ctx, created_at, updated_at) VALUES (:i, :p, :s, :ph, "
        "'Park bench map', :u, :n, CAST(:st AS JSONB), CAST(:f AS JSONB), :at, :at)"),
        {"i": rid, "p": pid, "s": status, "ph": "done" if status == "completed" else "write_tests",
         "u": f"http://boss-local/local_{pid}/park-bench-map/pull/{number}" if number else None,
         "n": number, "at": at, "st": json.dumps(state or BUILD_1_STATE),
         "f": json.dumps({**(fctx if fctx is not None else BUILD_1_FCTX),
                          "origin": {"decision_id": decision_id}})})
    return rid


def _check(db, pid: str, build_id: str, *, at: datetime) -> None:
    db.execute(text(
        "INSERT INTO acceptance_checks (id, project_id, lineage_root_run_id, code_task_run_id, layer, "
        "mode, verdict, counts, items, created_at) VALUES (:i, :p, :b, :b, 'build_gate', 'static', 'not_met', "
        "CAST(:c AS JSONB), CAST(:it AS JSONB), :at)"),
        {"i": _id("acc"), "p": pid, "b": build_id, "at": at,
         "c": json.dumps({"met": 4, "missing": 0, "partial": 2, "unjudged": 0}),
         "it": json.dumps(CHECK_ITEMS)})


@pytest.fixture
def bench(world, client, engine):
    """The Park bench map project as BOSS recorded it."""
    pid = _project(world, client, engine,
                   "A visitor taps a bench on a local map and reads the note stored for that bench.")
    t0 = datetime(2026, 11, 19, 7, 39, tzinfo=timezone.utc)
    with engine.begin() as db:
        dec1, run1 = _prd(db, pid, PRD_1, at=t0, by="auto_approval", synthesis=SYNTHESIS_1,
                          note="Approved by your Easy Mode start: you asked BOSS to build this, "
                               "and the panel's spec is what it built.")
        b1 = _build(db, pid, dec1, at=t0 + timedelta(minutes=1))
        _check(db, pid, b1, at=t0 + timedelta(minutes=7))
        dec2, run2 = _prd(db, pid, PRD_2, at=t0 + timedelta(hours=6), by=world.owner.id,
                          note="Resolved from the BOSS console.")
    return {"pid": pid, "dec1": dec1, "run1": run1, "build1": b1, "dec2": dec2, "run2": run2}


def _get(world, client, path: str) -> dict:
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_overview_says_what_the_app_is_from_its_prd(world, client, bench):
    ov = _get(world, client, f"/v1/projects/{bench['pid']}/overview")
    assert "about" not in (ov.get("errors") or {}), ov.get("errors")
    about = ov["about"]
    assert about["source"] == "prd"
    assert about["title"] == "Park Bench Map"
    assert about["summary"].startswith("Park Bench Map is a small single-service web app")
    assert "**" not in about["summary"] and "`" not in about["summary"]
    assert [c["status"] for c in about["criteria"]] == ["partial", "met", "met", "partial", "met", "met"]
    assert about["criteria"][0]["text"].startswith("Opening the app shows a MapLibre map")
    assert about["counts"] == {"met": 4, "partial": 2, "not_met": 0, "unchecked": 0}
    assert about["approved"]["words"] == "Approved by your Easy Mode start"
    assert about["approved"]["at"].startswith("2026-11-19T07:39")
    assert "area=meetings" in about["spec_href"] and f"meeting={bench['run1']}" in about["spec_href"]
    # The later spec a person approved is named, newest first, linked to its own meeting.
    assert [s["title"] for s in about["later"]] == ["Bench Name Label on Map Bubble"]
    assert f"meeting={bench['run2']}" in about["later"][0]["href"]


def test_criteria_no_check_has_judged_say_so(world, client, engine):
    pid = _project(world, client, engine, "A bench map.")
    with engine.begin() as db:
        _prd(db, pid, PRD_2, at=datetime(2026, 11, 19, 13, 42, tzinfo=timezone.utc),
             by=world.owner.id, note="Resolved from the BOSS console.")
    about = _get(world, client, f"/v1/projects/{pid}/overview")["about"]
    assert about["title"] == "Bench Name Label on Map Bubble"
    assert [c["status"] for c in about["criteria"]] == ["unchecked"] * 3
    assert about["approved"]["words"].startswith("Approved by ") and "auto" not in about["approved"]["words"]


def test_a_project_without_a_prd_shows_its_brief_and_says_so(world, client, engine):
    pid = _project(world, client, engine, "A visitor taps a bench and reads its note.")
    about = _get(world, client, f"/v1/projects/{pid}/overview")["about"]
    assert about["source"] == "brief"
    assert about["summary"] == "A visitor taps a bench and reads its note."
    assert about["note"] == "No spec yet — this is the project brief."
    assert about["criteria"] == [] and about["spec_href"] is None


def test_the_build_page_says_what_was_discussed_and_what_was_built(world, client, engine, bench):
    got = _get(world, client, f"/v1/projects/{bench['pid']}/builds/{bench['build1']}/explained")
    said = got["discussed"]
    assert said["decisions"][0].startswith("Stack line conflict")
    assert any(d.startswith("Service worker for offline: not building one") for d in said["decisions"])
    assert len(said["decisions"]) <= 4 and said["seats"] == 5
    assert said["open_questions"][0].startswith("Repo state unknown")
    # Decisions, never the transcript.
    assert TRANSCRIPT_LINE not in json.dumps(said)
    assert f"meeting={bench['run1']}" in said["href"]

    built = got["built"]
    bullets = built["bullets"]
    assert 3 <= len(bullets) <= 6, bullets
    assert any(b.startswith("Changed 17 files: server code 3") for b in bullets), bullets
    assert "Tests: 4 test files added or updated, all 15 tests pass" in bullets
    assert "Review: 2 review rounds, 3 findings raised, 4 of 6 acceptance criteria met, 2 partly" in bullets
    assert any(b.startswith("Left as follow-ups: benches_viewed_total") for b in bullets), bullets
    assert bullets[-1] == "Merged as change #1"
    # Files grouped by purpose, not a raw list.
    groups = {g["purpose"]: g["files"] for g in built["groups"]}
    assert groups["Tests"] == TEST_FILES
    assert groups["Deploy setup"] == ["Dockerfile", "boss.deploy.yaml", "boss.preview.command", "docker-compose.yml"]
    assert groups["Dependencies"] == ["package-lock.json", "package.json"]
    assert groups["Screens"] == ["public/app.js"] and groups["Data"] == ["data/benches.geojson"]


def test_the_explanation_is_stored_once_and_never_worked_out_again(world, client, engine, bench):
    path = f"/v1/projects/{bench['pid']}/builds/{bench['build1']}/explained"
    first = _get(world, client, path)["built"]
    with engine.begin() as db:
        kept = db.execute(text("SELECT finalize_ctx->'explained' FROM code_task_runs WHERE id = :r"),
                          {"r": bench["build1"]}).scalar()
        assert kept and kept["version"] >= 1 and kept["facts"]
        # The record moves on (a later repair rewrites the state): the stored words stand.
        db.execute(text("UPDATE code_task_runs SET state = state || '{\"files_changed\": 99}'::jsonb "
                        "WHERE id = :r"), {"r": bench["build1"]})
        panel = db.execute(text("SELECT finalize_ctx->'explained' FROM meeting_runs WHERE id = :m"),
                           {"m": bench["run1"]}).scalar()
        assert panel and panel["decisions"]
    again = _get(world, client, path)["built"]
    assert again["bullets"] == first["bullets"]


def test_a_build_still_running_is_explained_so_far_and_not_stored(world, client, engine, bench):
    with engine.begin() as db:
        running = _build(db, bench["pid"], bench["dec2"], at=datetime.now(timezone.utc), status="running",
                         number=None, state={"impl_files": ["public/app.js"], "test_files": []}, fctx={})
    got = _get(world, client, f"/v1/projects/{bench['pid']}/builds/{running}/explained")
    assert got["built"]["bullets"] == ["Changed 1 file: screens 1"]
    assert got["discussed"]["href"].endswith(bench["run2"]) or f"meeting={bench['run2']}" in got["discussed"]["href"]
    with engine.begin() as db:
        assert db.execute(text("SELECT finalize_ctx ? 'explained' FROM code_task_runs WHERE id = :r"),
                          {"r": running}).scalar() is False


def test_completion_stores_the_explanation(engine, bench):
    """Whichever path writes `completed`, the terminal hook (run_terminal) stores the words."""
    from boss_engine.services.run_terminal import on_status_written

    with engine.begin() as db:
        b = _build(db, bench["pid"], bench["dec1"], at=datetime.now(timezone.utc), number=2)
        on_status_written(db, b)
        kept = db.execute(text("SELECT finalize_ctx->'explained' FROM code_task_runs WHERE id = :r"),
                          {"r": b}).scalar()
    assert kept and any(f.startswith("Changed 17 files") for f in kept["facts"])


def test_the_work_drawer_carries_the_same_explanation(world, client, bench):
    story = _get(world, client, f"/v1/projects/{bench['pid']}/work/{bench['build1']}/story")
    got = story["explained"]
    assert got["built"]["build_id"] == bench["build1"]
    assert got["built"]["bullets"][-1] == "Merged as change #1"
    assert got["discussed"]["decisions"][0].startswith("Stack line conflict")


def test_an_unknown_build_is_a_404(world, client, bench):
    with world.acting_as(world.ctx(world.owner)):
        r = client.get(f"/v1/projects/{bench['pid']}/builds/crun_nope/explained")
    assert r.status_code == 404
