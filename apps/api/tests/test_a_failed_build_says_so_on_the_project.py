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
    from boss_engine.services.build_failure import failure_words

    said = failure_words(REAL_ERROR)
    assert said == ("It couldn't save its changes: git was not allowed to write to the build's "
                    "copy of the repo (permission denied).")
    trace = ("Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n"
             "RuntimeError: the preview never answered on port 8004 [substrate=docker]")
    assert failure_words(trace) == "The preview never answered on port 8004."
    assert failure_words("") == "BOSS didn't record why it stopped."
    assert "substrate" not in failure_words("boom [substrate=local_subprocess]")
    assert len(failure_words("x" * 400)) <= 161


@pytest.mark.parametrize("raw, gone", [
    ("git push failed: token ghs_ABCDEFGHIJKLMNOPQRSTUV1234 lacks scope", "ghs_"),
    ("git push failed: ghp_ABCDEFGHIJKLMNOPQRSTUV1234 refused", "ghp_"),
    ("clone failed with github_pat_11ABCDEFG0123456789_abcdefghijkl", "github_pat_"),
    ("BOSS could not run claude_code: Authorization: Bearer sk-ant-api03-abcdefghijklmnop; "
     "fix: reconnect it, then retry.", "sk-ant-"),
    ("model call failed: key sk-proj-abcdefghijklmnopqrstuvwx rejected", "sk-proj-"),
    ("fetch https://bob:hunter2@github.com/acme/app.git failed", "hunter2"),
    ("RuntimeError: secret sref_abc123def456 could not be resolved", "sref_"),
    ("FileNotFoundError: [Errno 2] No such file or directory: "
     "'/var/lib/boss/sandboxes/abc/crun_1/app/src/x.py'", "/var/lib/boss"),
    ("could not open /tmp/boss-build-7/cache/state.db", "/tmp/boss"),
    ("could not read /Users/param/.config/boss/creds.json", "/Users/param"),
    ("could not read /home/dev/.ssh/id_ed25519", "/home/dev"),
])
def test_a_reason_never_carries_a_secret_or_a_host_path(raw, gone):
    from boss_engine.services.build_failure import failure_words

    assert gone not in failure_words(raw)


def test_the_shared_redactor_masks_the_new_shapes_and_keeps_paths_in_logs():
    from boss_engine.identity.redaction import redact_text

    for token in ("ghs_ABCDEFGHIJKLMNOPQRSTUV1234", "github_pat_11ABCDEFG0123456789_abcdefgh",
                  "sk-ant-api03-abcdefghijklmnop", "sk-proj-abcdefghijklmnopqrstuvwx"):
        assert token not in redact_text(f"x {token} y")
    assert "hunter2" not in redact_text("https://bob:hunter2@github.com/a/b.git")
    assert "github.com/a/b.git" in redact_text("https://bob:hunter2@github.com/a/b.git")
    # A log keeps the path (operators need it); a sentence for people does not.
    path = "/var/lib/boss/sandboxes/abc/crun_1/app/src/x.py"
    assert path in redact_text(f"open {path}")
    assert redact_text(f"open '{path}'", public=True) == "open 'src/x.py'"
    assert "task-abcdefghijklmnopqrstuvwxyz" in redact_text("task-abcdefghijklmnopqrstuvwxyz")


def test_the_redactor_is_linear_on_adversarial_input():
    import time

    from boss_engine.identity.redaction import redact_text

    # It runs on every log record: a 100 KB run of `a.` (every dot a word boundary) once took
    # 11 s through an unbounded URL-scheme pattern.
    for blob in ("a." * 50_000, "-".join(["123e4567-e89b-12d3-a456-426614174000"] * 2800),
                 "x+" * 50_000):
        started = time.perf_counter()
        redact_text(blob)
        redact_text(blob, public=True)
        assert time.perf_counter() - started < 0.2, len(blob)
    assert "p@" not in redact_text("postgresql+psycopg://u:p@db:5432/boss")


def test_a_url_and_a_slug_survive_redaction():
    from boss_engine.identity.redaction import redact_text

    said = redact_text("see https://example.com/home/dashboard and /api/root/x and /rootfs/y",
                       public=True)
    assert said == "see https://example.com/home/dashboard and /api/root/x and /rootfs/y"
    for slug in ("feature/sk-1234-add-checkout-flow-page", "risk-sk-abcdefghijklmnopqrstu"):
        assert redact_text(slug) == slug
    assert "sk-svcacct" not in redact_text("key sk-svcacct-abcdefghijklmnopqrstuvwx")
    assert "a" * 40 not in redact_text("key sk-" + "a" * 40)


def test_reasons_read_well():
    from boss_engine.services.build_failure import failure_words

    git = ("git -c core.x failed: Cloning into '.'...\nremote: Repository not found.\n"
           "fatal: repository 'https://github.com/acme/app/' not found")
    assert failure_words(git) == "Git failed: Repository not found."
    assert failure_words("'implement'") == "It stopped unexpectedly at the implement step."
    assert failure_words("Claude code agent exited 143:") == "It was stopped (timed out or cancelled)."
    codex = ("BOSS could not run codex: 2026-11-17T02:00:27.579001Z ERROR codex_api::endpoint::ws: "
             "failed to connect to websocket: HTTP error 401; fix: sign in to codex again, then retry.")
    assert failure_words(codex) == "BOSS couldn't run codex: failed to connect to websocket: HTTP error 401."


NOT_LOGGED_IN = ("BOSS could not run claude_code: claude code agent exited 1: success — Not logged "
                 "in · Please run /login; fix: reconnect the Claude Code subscription in Repo and "
                 "models, then retry. [substrate=local_subprocess]")
JAVA_ONLY = ("This build needs Java, which is not turned on for this BOSS: turn it on with "
             "`boss toolchains enable java` on the BOSS host, then start again.")
JAVA_OFF = ("This build asks for Ruby or PHP, which BOSS cannot build. This build needs Java, "
            "which is not turned on for this BOSS: turn it on with `boss toolchains enable java` "
            "on the BOSS host, then start again.")


@pytest.mark.parametrize("kw, label, has_href", [
    ({"failure_class": "needs_human", "reason_code": "harness_unavailable", "error": NOT_LOGGED_IN},
     "Sign in again in Setup › Models", True),
    ({"failure_class": "needs_human", "reason_code": "harness_unavailable",
      "error": "BOSS could not run claude_code: 401 OAuth access token has expired; fix: "
               "reconnect it in Repo and models, then retry."},
     "Sign in again in Setup › Models", True),
    ({"failure_class": "needs_human", "reason_code": "stack_unsupported", "error": JAVA_ONLY},
     "Turn on Java", False),
    ({"failure_class": "needs_human", "reason_code": "build_services_unavailable",
      "error": "Build services unavailable: cannot reach the build-services Postgres"},
     "Start build services", True),
    ({"failure_class": "needs_human", "reason_code": "no_test_files",
      "error": "Test author produced no test files even after escalating"},
     "Make the brief testable", True),
    ({"failure_class": "budget", "reason_code": "budget_cap", "error": "It hit its spending cap."},
     "Raise the cap in Govern", True),
])
def test_a_failure_a_person_can_fix_names_the_fix_first_and_keeps_retry_for_after(kw, label, has_href):
    from boss_engine.services.build_failure import action_for

    got = action_for("p", source="code_task", run_id="crun_a", see_href="?see", **kw)
    # The fix's own words end "then retry": Retry stays, beside the fix, so the card is no dead end.
    assert got["retry"]["href"] == "/v1/code-tasks/crun_a/retry" and got["retry_reason"] is None
    assert got["fix"]["label"] == label and bool(got["fix"]["href"]) == has_href
    if label == "Turn on Java":
        assert got["fix"]["command"] == "boss toolchains enable java"
    if label == "Raise the cap in Govern":
        assert "area=budget" in got["fix"]["href"]


def test_a_stack_boss_cannot_build_is_said_so_never_turn_on_another_toolchain():
    from boss_engine.services.build_failure import action_for

    got = action_for("p", source="code_task", run_id="crun_a", failure_class="needs_human",
                     reason_code="stack_unsupported", error=JAVA_OFF, see_href="?see")
    assert got["fix"]["label"] == "See what it needs" and "Ruby or PHP" in got["fix"]["detail"]
    assert "Java" not in got["fix"]["label"] and got["retry"] is None
    assert "can't build Ruby or PHP" in got["retry_reason"]


def test_retry_only_when_it_can_help():
    from boss_engine.services.build_failure import action_for

    infra = action_for("p", source="code_task", run_id="crun_a", failure_class="infra",
                       reason_code="pipeline_exception", error=REAL_ERROR)
    assert infra["retry"] == {"run_id": "crun_a", "href": "/v1/code-tasks/crun_a/retry",
                              "method": "POST"} and infra["fix"] is None
    merged = action_for("p", source="code_task", run_id="crun_a", error="x", merged=True)
    assert merged["retry"] is None and "merged" in merged["retry_reason"]
    # A workflow's build is retried from the workflow's failed step, never a bare code task.
    wf = action_for("p", source="workflow_run", run_id="crun_a", error=REAL_ERROR, see_href="?wfr")
    assert wf["retry"] is None and wf["fix"]["href"] == "?wfr"


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
            {"i": run_id, "p": slug, "e": REAL_ERROR,
             "f": json.dumps({"failure_class": "infra", "reason_code": "pipeline_exception"}), "at": at,
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
    # Needs you leads with it: the reason, Retry and See what happened — and no generic
    # action that a needs drawer would turn into a link to the API URL (review L4).
    card = ov["needs_you"][0]
    assert card["action"] is None and card["fix"] is None
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
    # ONE RULE (review L1): the list, the page and the Command Center name the same failure.
    assert {k: card_row["build_failed"][k] for k in ("id", "build_id", "word", "reason", "retry")} \
        == {k: failed[k] for k in ("id", "build_id", "word", "reason", "retry")}


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


def _project(world, client, name="Recipe box"):
    slug = f"fb-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": name, "slug": slug})
    assert made.status_code == 201, made.text
    return slug


def _build(db, pid, *, status="failed", error=REAL_ERROR, fctx=None, title="Recipe box",
           minutes_ago=5, pr_url=None):
    rid = f"crun_{uuid.uuid4().hex[:20]}"
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, error, pull_request_url, "
        "finalize_ctx, created_at, updated_at) VALUES (:i, :p, :s, 'done', :t, :e, :u, "
        "CAST(:f AS JSONB), :at, :at)"),
        {"i": rid, "p": pid, "s": status, "t": title, "e": error, "u": pr_url,
         "f": json.dumps(fctx or {}), "at": at})
    return rid


def test_a_failure_only_a_person_can_fix_offers_the_fix_everywhere(world, client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import command_center

    pid = _project(world, client)
    with engine.begin() as db:
        run = _build(db, pid, error=NOT_LOGGED_IN,
                     fctx={"failure_class": "needs_human", "reason_code": "harness_unavailable"})
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    failed = ov["project"]["build_failed"]
    assert failed["build_id"] == run and failed["retry"]["run_id"] == run
    assert failed["fix"]["label"] == "Sign in again in Setup › Models"
    assert "area=setup" in failed["fix"]["href"]
    assert "login" not in failed["reason"].lower() or "Not logged in" in failed["reason"]
    card = ov["needs_you"][0]
    assert card["kind"] == "failed" and card["retry"] == failed["retry"] and card["fix"] == failed["fix"]
    with engine.connect() as c:
        cc = command_center.build(c, get_settings(), pid)
    item = next(n for n in cc["needs_you"] if n["kind"] == "build_failed")
    # The fix rides on the card, and Retry beside it for after the fix.
    assert [a["key"] for a in item["answers"]] == ["retry"] and item["build"]["retryable"] is True
    assert item["build"]["fix"]["label"] == failed["fix"]["label"]


def test_a_user_cancelled_latest_attempt_is_not_a_failure(world, client, engine):
    pid = _project(world, client)
    with engine.begin() as db:
        root = _build(db, pid, minutes_ago=30)
        _build(db, pid, status="cancelled", error=None, minutes_ago=2,
               fctx={"root_run_id": root, "parent_run_id": root, "attempt": 2})
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    assert ov["project"]["build_failed"] is None
    assert not any(n["kind"] == "failed" for n in ov["needs_you"])
    row = next(c for c in _get(world, client, "/v1/projects/summary")["items"] if c["id"] == pid)
    assert row["build_failed"] is None


def test_a_merged_change_that_later_failed_offers_no_retry(world, client, engine):
    pid = _project(world, client)
    with engine.begin() as db:
        _build(db, pid, error="post-merge cleanup failed",
               fctx={"failure_class": "infra", "merge": {"sha": "abc1234"}})
    failed = _get(world, client, f"/v1/projects/{pid}/overview")["project"]["build_failed"]
    assert failed["retry"] is None and "merged" in failed["retry_reason"]


def test_a_workflow_owned_build_defers_to_the_workflow_run(world, client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services import command_center
    from apps.api.tests.project_experience_seed import _minor_feature

    pid = _project(world, client)
    with engine.begin() as db:
        did, vid, _spec = _minor_feature(db, pid)
        wfr = f"wfr_{uuid.uuid4().hex[:16]}"
        at = datetime.now(timezone.utc) - timedelta(minutes=20)
        db.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
            "actor, status, input, error, created_at, updated_at) VALUES (:i, :p, :d, :v, 'ceo', "
            "'failed', CAST(:in AS JSONB), :e, :t, :t)"),
            {"i": wfr, "p": pid, "d": did, "v": vid, "e": REAL_ERROR, "t": at,
             "in": json.dumps({"title": "Recipe box", "directive": "Recipe box"})})
        build = _build(db, pid, fctx={"workflow_run_id": wfr, "failure_class": "infra"},
                       minutes_ago=10)
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    failed = ov["project"]["build_failed"]
    assert failed["source"] == "workflow_run" and failed["id"] == wfr
    # Never a bare code-task retry of a workflow's build: the step Retry on the run's page.
    assert failed["retry"] is None and failed["build_id"] == build
    assert failed["see_href"] == f"?project_id={pid}&area=delivery_workflows&wfr={wfr}"
    assert failed["fix"]["href"] == failed["see_href"]
    assert ov["needs_you"][0]["retry"] is None
    with engine.connect() as c:
        cc = command_center.build(c, get_settings(), pid)
    # The Command Center leaves it to the run's failed-step card: no bare build card.
    assert not any(n["kind"] == "build_failed" for n in cc["needs_you"])


def test_on_a_live_project_the_failure_sits_beside_the_stage(world, client, engine):
    from apps.api.tests.project_experience_seed import seed

    pid = _project(world, client, name="HelpDesk AI Demo")
    with engine.begin() as db:
        seed(db, pid)
        _build(db, pid, title="Follow-up: tidy the footer", minutes_ago=0)
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    failed = ov["project"]["build_failed"]
    assert failed is not None and failed["beside"] is True
    assert ov["project"]["status_label"] not in ("Build failed", "Follow-up failed")
    working = next(n for n in ov["journey"] if n["key"] == "working")
    assert working["state"] != "failed"
    assert ov["needs_you"][0]["kind"] == "failed"


def test_a_completed_build_with_a_failed_follow_up_retries_the_follow_up(world, client, engine):
    """The laptop's lineage (2026-11-18): crun_d8b… completed (change #1 open, merge pending) and
    its automatic follow-up crun_47c3… failed. Retry resumes the FAILED attempt — the one a
    retry can resume — and the merge still pending reads waiting (amber), never a red ✗."""
    pid = _project(world, client)
    dec = f"dec_{uuid.uuid4().hex[:16]}"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
            "created_at) VALUES (:i, :p, 'Merge change #1?', CAST(:r AS JSONB), 'pending', 2, now())"),
            {"i": dec, "p": pid, "r": json.dumps({"action": "merge_pr"})})
        root = _build(db, pid, status="completed", error=None, minutes_ago=30,
                      pr_url="http://boss-local/local_x/recipe-box/pull/1",
                      fctx={"app_id": "app_rb"})
        db.execute(text("UPDATE code_task_runs SET decision_id = :d WHERE id = :i"),
                   {"d": dec, "i": root})
        follow = _build(db, pid, title="Follow-up: deferred review findings — Recipe box",
                        minutes_ago=5, fctx={"root_run_id": root, "parent_run_id": root,
                                             "app_id": "app_rb", "failure_class": "infra"})
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    failed = ov["project"]["build_failed"]
    assert failed["id"] == root and failed["build_id"] == follow
    assert failed["word"] == "Follow-up failed"
    assert failed["retry"]["run_id"] == follow and failed["see_href"].endswith(f"build={follow}")
    assert ov["needs_you"][0]["retry"]["run_id"] == follow
    row = next(w for w in ov["recent_work"] if w["id"] == root)
    assert {s["key"]: s["state"] for s in row["stages"]}["pr"] == "waiting"
    story = _get(world, client, f"/v1/projects/{pid}/work/{root}/story")
    merge = next(s for s in story["stages"] if s["key"] == "pr")
    assert merge["state"] == "waiting" and merge["state"] != "failed"


def test_a_failed_build_a_later_build_replaced_is_not_a_red_failure(world, client, engine):
    """Easy Mode's relaunch is a new root for the same app: the first build's failure is
    history once it exists — "Replaced by a later build", never red Failed beside it."""
    pid = _project(world, client)
    with engine.begin() as db:
        first = _build(db, pid, minutes_ago=90, fctx={"app_id": "app_rb", "failure_class": "infra"})
        later = _build(db, pid, status="completed", error=None, minutes_ago=30,
                       fctx={"app_id": "app_rb"})
    ov = _get(world, client, f"/v1/projects/{pid}/overview")
    row = next(w for w in ov["recent_work"] if w["id"] == first)
    assert row["state"] == "done" and row["replaced_by"]["id"] == later
    assert not any(st["state"] == "failed" for st in row["stages"])
    assert ov["project"]["build_failed"] is None
