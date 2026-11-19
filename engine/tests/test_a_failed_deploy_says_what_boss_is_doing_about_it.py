"""A failed deploy BOSS is fixing says so — and a crash on start is said, and caught, as one.

THE CASE (laptop, Recipe box, sdep_73c0758fb5a143fa81c2, 2026-11-19). The deploy's health check
failed: the container had exited in its first second on `node server.js` →
`Error: Cannot find module '/app/server.js'`, but the probe waited the full 600 s and the
headline said "did not answer as expected … within 600s". Two minutes later the deploy step's
agent (Pilot) decided fix_app; a repair build ran 02:54-03:00; the fixed version was planned at
03:02 (sdep_5be0890e) and waited for approval on a card reading "Deploy
local_recipe-box-fd3ab4a3/recipe-box@095121229a40 to 2.29.2.202 (/opt/boss-apps/recipe-box)?".
Through all of it, every project surface said "Last deploy failed" in red, and the operator
asked why no agent was fixing it.

Now:
  * the health probe stops when the app's containers have all exited, and the headline is the
    app's own error ("The app crashed on start (Error: Cannot find module …)");
  * deploy_repair.underway is the one answer to "what is BOSS doing about this failed deploy",
    read by the Deployments card, its history, the header chip and the agents' list_deployments:
    fixing (a repair build), looking (the agent reading the log), retrying (a backoff), the fix
    waiting for approval — and nothing (still "Last deploy failed") when it is a person's;
  * the fix's deploy card says it is the fix, in plain words.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.services import deploy_repair, deploy_truth, stack_deploy as sd
from boss_engine.services.project_experience import deployments

from .test_a_target_is_live_only_after_it_deploys import (  # noqa: F401 - fixture
    _forget_workflows,
    _target,
    db,
)

#: sdep_73c0758fb5a143fa81c2's health step, as the runner logged it.
RECIPE_HEALTH_LOG = """
$ [health]
recipe-box-web-1 Exited (1) 7 minutes ago
--- recipe-box-web-1 (last lines) ---

> recipe-box@1.0.0 start
> node server.js

node:internal/modules/cjs/loader:1433
  throw err;
  ^

Error: Cannot find module '/app/server.js'
    at Function._resolveFilename (node:internal/modules/cjs/loader:1430:15)
    at defaultResolveImpl (node:internal/modules/cjs/loader:1040:19)
  code: 'MODULE_NOT_FOUND',
  requireStack: []
}

Node.js v22.23.5
"""
URL = "http://2.29.2.202:8014"


# ── the crash, said and caught ───────────────────────────────────────────────────────────────

def test_a_crash_on_start_is_said_in_the_apps_own_words_and_is_the_apps_to_fix():
    said = sd.explain_failure("health", 1, "BOSS_APP_EXITED 000\n" + RECIPE_HEALTH_LOG,
                              host="2.29.2.202", url=URL, timeout=600)
    assert said["cause"] == sd.CAUSE_APP
    assert said["headline"] == ("The app crashed on start (Error: Cannot find module "
                                f"'/app/server.js'), so nothing answered at {URL}.")
    # Other runtimes' last words too.
    for line in ("ModuleNotFoundError: No module named 'flask'",
                 "panic: runtime error: invalid memory address",
                 'Exception in thread "main" java.lang.IllegalStateException: no config'):
        out = f"web-1 Exited (1)\n--- web-1 (last lines) ---\nTraceback:\n  x\n{line}\n"
        assert sd.crash_line(out) == line, line


def test_an_app_that_never_answered_but_did_not_crash_keeps_the_timeout_words():
    said = sd.explain_failure("health", 1, "BOSS_UNHEALTHY 502", host="h", url=URL, timeout=600)
    assert said["headline"].startswith("The app did not answer as expected")
    assert said["cause"] == sd.CAUSE_APP
    # A container still running that logged an error is not a crash on start.
    assert sd.crash_line("--- web-1 (last lines) ---\nError: transient\n") == ""


def test_the_health_probe_stops_waiting_when_every_container_of_the_app_exited():
    runner = next(c for c in vars(sd).values()
                  if isinstance(c, type) and hasattr(c, "_crash_probe"))
    composed = SimpleNamespace(mode="install", rendered={"install": [
        {"run": "docker compose -p recipe-box down"},
        {"run": "docker compose -p recipe-box up -d --build"}]})
    probe = runner._crash_probe(composed)
    assert "label=com.docker.compose.project=recipe-box" in probe
    assert "status=running" in probe and sd.APP_EXITED in probe
    assert "-ge 20" in probe and "$down -ge 3" in probe, "a grace, then three looks in a row"
    # Without compose there is nothing to look at: only the deadline ends the wait.
    plain = SimpleNamespace(mode="install", rendered={"install": [{"run": "./start.sh"}]})
    assert runner._crash_probe(plain) == ""


def test_the_fixs_deploy_card_says_it_is_the_fix():
    asked = sd.fix_question(
        {"cause": "The app crashed on start (Error: Cannot find module '/app/server.js'), so "
                  f"nothing answered at {URL}.", "build_run_id": "crun_68a2"},
        app="Recipe box", short="0951212", environment="staging", host="2.29.2.202")
    assert asked == ("BOSS fixed the Recipe box deploy: the app crashed on start (Error: Cannot "
                     f"find module '/app/server.js'), so nothing answered at {URL}. Deploy the "
                     "fix (0951212) to staging on 2.29.2.202?")


# ── what BOSS is doing about it, on every surface ────────────────────────────────────────────

def _run(c, db) -> str:
    did, vid, rid = (f"wfd_{uuid.uuid4().hex[:10]}", f"wfv_{uuid.uuid4().hex[:10]}",
                     f"wfr_{uuid.uuid4().hex[:10]}")
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   " created_by) VALUES (:i, :p, :k, 'Ship', 'ceo')"),
              {"i": did, "p": db.project, "k": did})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                   " version, spec, content_hash, created_by) VALUES (:i, :p, :d, 1, "
                   " '{}'::jsonb, 'h', 'ceo')"), {"i": vid, "p": db.project, "d": did})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   " workflow_version_id, actor, status) VALUES (:i, :p, :d, :v, 'ceo', "
                   " 'waiting')"), {"i": rid, "p": db.project, "d": did, "v": vid})
    return rid


def _deployment(c, db, target, run_id, status, *, at, build="crun_first"):
    """A stack deployment shaped like sdep_73c0758f (failed) or sdep_5be0890e (the fix)."""
    did = f"sdep_{uuid.uuid4().hex[:12]}"
    failed = status == "failed"
    headline = ("The app crashed on start (Error: Cannot find module '/app/server.js'), so "
                f"nothing answered at {URL}.")
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
        " commit_sha, status, steps, log_tail, failure_reason, error, created_by, created_at, "
        " finished_at, workflow_run_id, code_task_run_id) VALUES (:i, :p, :t, :h, "
        " 'local_recipe-box/recipe-box', 'pull/3', :c, :s, '[]'::jsonb, :log, :r, "
        " CAST(:e AS JSONB), 'ceo', :at, :fin, :w, :b)"),
        {"i": did, "p": db.project, "t": target, "h": db.host, "c": uuid.uuid4().hex,
         "s": status, "log": RECIPE_HEALTH_LOG if failed else "",
         "r": headline if failed else None,
         "e": json.dumps({"step": "health", "cause": "app", "headline": headline,
                          "action": "Fix it."}) if failed else None,
         "at": at, "fin": at if failed else None, "w": run_id, "b": build})
    return did


def _step(c, db, run_id, status, output):
    c.execute(text(
        "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
        " status, output, binding_snapshot, attempt) VALUES (:i, :p, :r, 'deploy', "
        " 'capability', :s, CAST(:o AS JSONB), '{\"capability\": \"deploy\"}'::jsonb, 1)"),
        {"i": f"wfs_{uuid.uuid4().hex[:10]}", "p": db.project, "r": run_id, "s": status,
         "o": json.dumps({"capability": "deploy", **output})})


def _build(c, db, status) -> str:
    rid = f"crun_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec) VALUES "
                   "(:i, :p, 'Recipe box', :s, '{}'::jsonb)"),
              {"i": rid, "p": db.project, "s": status})
    return rid


def _fix(deployment_id, build):
    return {"host": "2.29.2.202", "phase": "apply", "attempt": 1, "build_run_id": build,
            "deployment_id": deployment_id, "from_build_run_id": "crun_first",
            "cause": "The app crashed on start (Error: Cannot find module '/app/server.js')"}


def _told(c, db, target):
    return deploy_truth.truth(c, deploy_truth.project_targets(c, db.project))[target]


def test_a_repair_build_in_flight_reads_boss_is_fixing_the_deploy_everywhere(db):
    now = datetime.now(timezone.utc)
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Recipe box", 8014, kind="stack")
            run_id = _run(c, db)
            failed = _deployment(c, db, target, run_id, "failed", at=now - timedelta(minutes=3))
            build = _build(c, db, "running")
            _step(c, db, run_id, "waiting", {
                "stack_deployment_id": failed, "deploy_fixes": [_fix(failed, build)],
                "await": {"id": build, "kind": "code_task", "then": "redeploy", "attempt": 1}})
            told = _told(c, db, target)
            chip = deploy_truth.project_deploy(c, db.project)
            view = deployments.view(c, db.project)
            agents = deploy_truth.targets_view(c, db.project)
        assert told["state"] == deploy_truth.FIXING, told
        assert told["word"] == "BOSS is fixing the deploy"
        assert "Cannot find module '/app/server.js'" in told["reason"] and build in told["reason"]
        assert told["repair"] == {"state": "fixing", "build_run_id": build}
        assert told["progress"] is not None, "the failed phases still show"
        assert chip["state"] == "fixing" and chip["word"] == "BOSS is fixing the deploy"
        row = next(h for h in view["history"] if h["id"] == failed)
        assert (row["state"], row["state_word"], row["tone"]) == (
            "now", "BOSS is fixing the deploy", "amber")
        assert row["repair"]["build_run_id"] == build
        assert agents[0]["word"] == "BOSS is fixing the deploy"
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_the_fix_waiting_for_approval_says_it_is_the_fix(db):
    now = datetime.now(timezone.utc)
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Recipe box", 8014, kind="stack")
            run_id = _run(c, db)
            failed = _deployment(c, db, target, run_id, "failed", at=now - timedelta(minutes=20))
            build = _build(c, db, "completed")
            fixed = _deployment(c, db, target, run_id, "awaiting_approval",
                                at=now - timedelta(minutes=9), build=build)
            _step(c, db, run_id, "waiting", {
                "stack_deployment_id": fixed, "deploy_fixes": [_fix(failed, build)],
                "redeploy": {"attempt": 1, "build_run_id": build},
                "await": {"id": fixed, "kind": "stack_deployment"}})
            told = _told(c, db, target)
            view = deployments.view(c, db.project)
        assert told["state"] == deploy_truth.WAITING
        assert told["word"] == "Fixed · waiting for your OK"
        assert told["reason"].startswith("BOSS fixed the app (The app crashed on start")
        rows = {h["id"]: h for h in view["history"]}
        assert rows[failed]["state_word"] == "Fixed by BOSS" and rows[failed]["tone"] == "amber"
        assert rows[failed]["repair"] == {"state": "fixed", "build_run_id": build,
                                          "next_deployment_id": fixed}
        assert rows[fixed]["state_word"] == "Fixed · waiting for your OK"
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_a_retry_after_a_backoff_and_the_agent_reading_the_log_are_said(db):
    now = datetime.now(timezone.utc)
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Recipe box", 8014, kind="stack")
            run_id = _run(c, db)
            failed = _deployment(c, db, target, run_id, "failed", at=now - timedelta(minutes=1))
            # Just failed: the step still waits on it, and the sweep wakes its agent.
            _step(c, db, run_id, "waiting", {"stack_deployment_id": failed,
                                              "await": {"id": failed,
                                                        "kind": "stack_deployment"}})
            looking = _told(c, db, target)
            c.execute(text("UPDATE workflow_step_runs SET output = output || CAST(:o AS JSONB) "
                           "WHERE workflow_run_id = :r"),
                      {"r": run_id, "o": json.dumps({"await": {
                          "kind": "recovery", "then": "run_again", "action": "retry",
                          "not_before": "2026-11-19T03:10:00+00:00"}})})
            retrying = _told(c, db, target)
        assert looking["state"] == "fixing" and looking["word"] == "BOSS is looking into it"
        assert retrying["state"] == "fixing" and retrying["word"] == "BOSS will deploy again"
        assert "at 03:10 UTC" in retrying["reason"]
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_a_failure_nobody_is_acting_on_stays_a_persons_last_deploy_failed(db):
    now = datetime.now(timezone.utc)
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Recipe box", 8014, kind="stack")
            run_id = _run(c, db)
            failed = _deployment(c, db, target, run_id, "failed", at=now - timedelta(minutes=2))
            # The agent asked a person (a host credential, a port another app holds): the step
            # failed with its question. No fixing words, no loop.
            _step(c, db, run_id, "failed", {"stack_deployment_id": failed,
                                             "error": "Port 8014 is held by another app."})
            asked = _told(c, db, target)
            # A step still parked on a failure long past: nobody is acting on it.
            c.execute(text("UPDATE workflow_step_runs SET status = 'waiting', "
                           "output = output || CAST(:o AS JSONB) WHERE workflow_run_id = :r"),
                      {"r": run_id, "o": json.dumps({"await": {"id": failed,
                                                               "kind": "stack_deployment"}})})
            c.execute(text("UPDATE stack_deployments SET finished_at = now() - interval '2 hours' "
                           "WHERE id = :i"), {"i": failed})
            stale = _told(c, db, target)
            # A repair build that failed: the step decides again or fails — not "fixing".
            build = _build(c, db, "failed")
            c.execute(text("UPDATE workflow_step_runs SET output = output || CAST(:o AS JSONB) "
                           "WHERE workflow_run_id = :r"),
                      {"r": run_id, "o": json.dumps({"deploy_fixes": [_fix(failed, build)]})})
            repair_failed = _told(c, db, target)
            # A deploy outside any workflow has no step to read.
            other = _target(c, db, "Notes", 8015, kind="stack")
            _deployment(c, db, other, None, "failed", at=now)
            loose = _told(c, db, other)
        for told in (asked, stale, repair_failed, loose):
            assert told["state"] == "failed" and told["word"] == "Last deploy failed", told
            assert told["repair"] is None
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_underway_reads_only_what_it_is_given():
    assert deploy_repair.underway(None, []) == {}
    assert deploy_repair.underway(None, [{"id": "sdep_x", "status": "healthy",
                                          "workflow_run_id": "wfr_x"}]) == {}
