"""BOSSAPP-930 / 932: one truth for a project's deployment targets, and a deploy you can watch.

From the laptop's Hello World (paramg/boss-sandbox, wfr_0b799972, 2026-11-08):
  * the Deploy step's attempt 1 said "No such deployment target in this project. Pick a stack
    target in Infra." about the target it had JUST made — on its own uncommitted connection,
    invisible to the planner's transaction — while the Deployments tab showed it;
  * the header and the card linked http://2.29.2.202:8007 as live while the only deploy there
    had failed, with the status "Unknown";
  * onboarding's "Use for staging" slot ("boss-sandbox", :8006, never deployed) showed as a
    second staging environment beside the app's own target;
  * nothing showed the deploy's progress while it ran.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import deploy_truth
from boss_engine.services.deploy_truth import phase_of, progress, truth
from boss_engine.services.stack_deploy import StackDeployError, plan_stack_deploy
from boss_engine.services.workflow_capability_runner import deploy_step_target

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")
ADDRESS = f"192.0.2.{uuid.uuid4().int % 250 + 2}"
T0 = datetime(2026, 11, 9, 0, 39, 44, tzinfo=timezone.utc)

#: sdep_f86f5344's steps, as the runner recorded them.
HELLO_STEPS = [
    {"name": "prepare", "status": "succeeded", "started_at": "2026-11-09T00:39:45+00:00",
     "ended_at": "2026-11-09T00:39:48+00:00"},
    {"name": "upload", "status": "succeeded", "started_at": "2026-11-09T00:39:48+00:00",
     "ended_at": "2026-11-09T00:39:54+00:00"},
    {"name": "environment", "status": "succeeded", "started_at": "2026-11-09T00:39:54+00:00",
     "ended_at": "2026-11-09T00:39:56+00:00"},
    {"name": "switch", "status": "succeeded", "started_at": "2026-11-09T00:39:56+00:00",
     "ended_at": "2026-11-09T00:39:58+00:00"},
    {"name": "retire previous single-container release", "status": "succeeded",
     "started_at": "2026-11-09T00:39:58+00:00", "ended_at": "2026-11-09T00:40:00+00:00"},
    {"name": "install: install 1", "status": "succeeded",
     "started_at": "2026-11-09T00:40:00+00:00", "ended_at": "2026-11-09T00:40:02+00:00"},
    {"name": "health", "status": "failed", "started_at": "2026-11-09T00:40:02+00:00",
     "ended_at": "2026-11-09T00:50:08+00:00"},
    {"name": "finalize", "status": "pending", "started_at": None, "ended_at": None},
]
HELLO_LOG = ("\n$ [install: install 1]\nbash: line 1: cd: hello-api: No such file or directory\n"
             "\n$ [health]\nBOSS_UNHEALTHY 000")


# ── the seven phases ─────────────────────────────────────────────────────────────────────────

def test_every_recorded_step_belongs_to_one_of_seven_phases():
    assert [phase_of(s["name"]) for s in HELLO_STEPS] == [
        "prepare", "upload", "environment", "switch", "switch", "install", "health", "smoke"]
    assert phase_of("install or update: npm ci") == "install"
    assert phase_of("retire this app's earlier release (shop)") == "switch"


def test_a_failed_deploy_says_which_phase_failed_with_its_own_log():
    p = progress(HELLO_STEPS, "failed", log_tail=HELLO_LOG)
    assert [x["key"] for x in p["phases"]] == list(deploy_truth.PHASE_KEYS)
    assert p["total"] == 7 and p["index"] == 6 and p["failed_phase"] == "health"
    by = {x["key"]: x for x in p["phases"]}
    assert by["switch"]["status"] == "done" and by["switch"]["elapsed_s"] == 4
    assert by["health"]["status"] == "failed" and by["health"]["elapsed_s"] == 606
    assert by["smoke"]["status"] == "pending"
    assert "No such file or directory" in by["install"]["log"]
    assert by["health"]["log"] == "BOSS_UNHEALTHY 000"


def test_a_running_deploy_counts_the_phase_it_is_in():
    steps = [dict(s) for s in HELLO_STEPS[:3]] + [
        {"name": "switch", "status": "running", "started_at": "2026-11-09T00:39:56+00:00",
         "ended_at": None}, {"name": "health", "status": "pending"},
        {"name": "finalize", "status": "pending"}]
    now = datetime(2026, 11, 9, 0, 40, 6, tzinfo=timezone.utc)
    p = progress(steps, "running", now=now)
    assert (p["index"], p["total"], p["current"]) == (4, 6, "switch"), p
    assert next(x for x in p["phases"] if x["key"] == "install")["status"] == "skipped"
    assert next(x for x in p["phases"] if x["key"] == "switch")["elapsed_s"] == 10


# ── live only after a deploy succeeded ───────────────────────────────────────────────────────

def test_an_address_is_a_live_url_only_after_a_deploy_there_succeeded(db):
    with db.engine.begin() as c:
        quiet = _target(c, db, "Quiet", 8010)
        failed = _target(c, db, "Hello", 8007)
        _sdep(c, db, failed, "failed", steps=HELLO_STEPS, log=HELLO_LOG,
              reason="The app did not answer as expected at http://x:8007 within 600s.")
        setting_up = _target(c, db, "Setup", 8011)
        _sdep(c, db, setting_up, "running", steps=HELLO_STEPS[:3] + [
            {"name": "switch", "status": "running", "started_at": T0.isoformat()}] + [
            {**s, "status": "pending", "started_at": None, "ended_at": None}
            for s in HELLO_STEPS[4:]])
        waiting = _target(c, db, "Waits", 8012)
        _sdep(c, db, waiting, "awaiting_approval")
        live = _target(c, db, "Live", 8013)
        _sdep(c, db, live, "healthy", at=T0)
        regressed = _target(c, db, "Regressed", 8014)
        _sdep(c, db, regressed, "healthy", at=T0)
        _sdep(c, db, regressed, "failed", at=T0 + timedelta(hours=1), reason="health failed")
        told = truth(c, deploy_truth.project_targets(c, db.project))

    assert told[quiet]["state"] == "not_deployed" and told[quiet]["word"] == "Not deployed yet"
    assert told[quiet]["live_url"] is None and told[quiet]["address"].endswith(":8010")
    assert told[failed]["state"] == "failed" and told[failed]["word"] == "Last deploy failed"
    assert told[failed]["live_url"] is None, "a failed first deploy is never a live link"
    assert "did not answer" in told[failed]["reason"]
    assert told[failed]["progress"]["failed_phase"] == "health"
    assert told[setting_up]["word"] == "Setting up · 4/7" and told[setting_up]["live_url"] is None
    assert told[waiting]["word"] == "Setting up · waiting for approval"
    assert told[live]["state"] == "live" and told[live]["live_url"].endswith(":8013")
    # Live before, failed since: the old release still answers, and the failure is said.
    assert told[regressed]["state"] == "failed" and told[regressed]["live_url"]


def test_the_deployments_card_and_header_never_link_an_undeployed_target(db):
    from boss_engine.services.project_experience import deployments, health

    with db.engine.begin() as c:
        failed = _target(c, db, "Hello", 8007)
        _sdep(c, db, failed, "failed", steps=HELLO_STEPS, reason="health failed")
        view = deployments.view(c, db.project)
        envs = health.environments(c, db.project)
    card = view["environments"][0]
    assert card["url"] is None and card["address"].endswith(":8007")
    assert card["deploy"]["word"] == "Last deploy failed"
    assert card["deploy"]["progress"]["failed_phase"] == "health"
    assert view["ship_path"][0]["state"] == "todo", "a failed deploy shipped nothing"
    assert deployments.live_url(envs) is None, "the header's live chip reads this"
    assert envs[0]["deploy"]["state"] == "failed"


# ── one staging target per app ───────────────────────────────────────────────────────────────

def test_an_unused_repository_slot_is_hidden_and_then_retired_once_the_app_has_its_own(db):
    with db.engine.begin() as c:
        slot = _target(c, db, "boss-sandbox", 8006, repo="paramg/boss-sandbox", kind="stack")
        own = _target(c, db, "Hello World REST API + sample", 8007)
        _sdep(c, db, own, "failed")
        listed = [t["id"] for t in deploy_truth.project_targets(c, db.project)]
        assert listed == [own], "the slot is not a second staging environment"
        assert deploy_truth.retire_superseded(c, db.project, actor="test") == [slot]
        assert c.execute(text("SELECT status FROM deployment_targets WHERE id = :i"),
                         {"i": slot}).scalar() == "retired"


def test_a_slot_that_shipped_is_an_app_and_is_never_hidden(db):
    with db.engine.begin() as c:
        slot = _target(c, db, "boss-sandbox", 8006, repo="paramg/boss-sandbox", kind="stack")
        _sdep(c, db, slot, "healthy")
        own = _target(c, db, "Hello", 8007)
        assert {t["id"] for t in deploy_truth.project_targets(c, db.project)} == {slot, own}


def test_the_apps_first_deploy_adopts_the_slot_instead_of_making_a_second(db):
    with db.engine.begin() as c:
        slot = _target(c, db, "boss-sandbox", 8006, repo="acme/boss-sandbox", kind="stack",
                       manifest="boss.deploy.yaml")
        run_id = _build(c, db.project, ["hello-api/serve_hello.py", "hello-api/boss.deploy.yaml"],
                        app="Hello World REST API")
        asked = deploy_truth_ask(c, db, run_id)
        assert asked["id"] == slot and asked["pending_create"], "the gate names the slot"
        assert c.execute(text("SELECT app_name FROM deployment_targets WHERE id = :i"),
                         {"i": slot}).scalar() == "boss-sandbox", "asking never writes"
    with db.engine.begin() as c:
        made = deploy_step_target(c, db.engine, db.project,
                                  {"run_id": run_id, "host_id": db.host})
    assert made["id"] == slot and made["manifest_path"] == "hello-api/boss.deploy.yaml"
    with db.engine.begin() as c:
        rows = c.execute(text("SELECT id, app_name, manifest_path FROM deployment_targets "
                              "WHERE project_id = :p"), {"p": db.project}).mappings().all()
    assert [(r["id"], r["app_name"], r["manifest_path"]) for r in rows] == [
        (slot, "Hello World REST API", "hello-api/boss.deploy.yaml")], rows


def test_use_for_staging_refuses_a_second_staging_target(db):
    from boss_engine.services.project_experience import onboarding

    with db.engine.begin() as c:
        _target(c, db, "Hello", 8007)
        with pytest.raises(StackDeployError) as refused:
            onboarding.use_for_staging(c, db.project, host_id=db.host, actor="test")
    assert "already deploys to staging" in refused.value.headline


def test_a_second_stack_target_for_the_same_manifest_is_refused(db):
    from boss_engine.services.stack_deploy import create_stack_target

    with db.engine.begin() as c:
        create_stack_target(c, project_id=db.project, host_id=db.host, app_name="hello",
                            repository="acme/boss-sandbox", environment="staging",
                            manifest_path="hello-api/boss.deploy.yaml", port=8020, actor="t")
        with pytest.raises(StackDeployError) as refused:
            create_stack_target(c, project_id=db.project, host_id=db.host,
                                app_name="hello-again", repository="acme/boss-sandbox",
                                environment="staging",
                                manifest_path="hello-api/boss.deploy.yaml", port=8021, actor="t")
    assert refused.value.status_code == 409 and "one target per environment" in \
        refused.value.action


# ── the Deploy step finds the target it made, and says which one it looked for ───────────────

def test_the_deploy_step_commits_the_target_it_makes_before_the_planner_reads_it(db):
    with db.engine.begin() as c:
        run_id = _build(c, db.project, ["notes/server.mjs", "notes/boss.deploy.yaml"],
                        app="Notes")
    # The step's own connection stays open, uncommitted, the way the runner holds it.
    with db.engine.connect() as step:
        step.begin()
        made = deploy_step_target(step, db.engine, db.project,
                                  {"run_id": run_id, "host_id": db.host})
        assert made and made["id"]
        # What plan_stack_deploy does: a transaction of its own.
        with db.engine.begin() as planner:
            seen = planner.execute(text("SELECT project_id FROM deployment_targets "
                                        "WHERE id = :i"), {"i": made["id"]}).scalar()
        step.rollback()
    assert seen == db.project, "the planner sees the target the step made"


def test_a_missing_target_is_named_with_the_reason(db):
    with db.engine.begin() as c:
        retired = _target(c, db, "Old", 8030, status="retired")
    with pytest.raises(StackDeployError) as gone:
        plan_stack_deploy(db.engine, None, project_id=db.project, target_id="deptgt_nothere",
                          repository="acme/x", ref="main", actor="t")
    assert "deptgt_nothere" in gone.value.headline and "no target with that id" in \
        gone.value.headline
    with pytest.raises(StackDeployError) as elsewhere:
        plan_stack_deploy(db.engine, None, project_id="some-other-project", target_id=retired,
                          repository="acme/x", ref="main", actor="t")
    assert retired in elsewhere.value.headline and db.project in elsewhere.value.headline


def test_agents_read_the_same_truth(db):
    with db.engine.begin() as c:
        slot = _target(c, db, "boss-sandbox", 8006, repo="paramg/boss-sandbox", kind="stack")
        own = _target(c, db, "Hello", 8007)
        _sdep(c, db, own, "failed", reason="health failed")
        seen = deploy_truth.targets_view(c, db.project)
    assert [t["target_id"] for t in seen] == [own] and slot not in str(seen)
    assert seen[0]["word"] == "Last deploy failed" and seen[0]["live_url"] is None


# ── fixtures ────────────────────────────────────────────────────────────────────────────────

def deploy_truth_ask(c, db, run_id):
    from boss_engine.services.stack_deploy import stack_target_for_deploy

    return stack_target_for_deploy(c, db.project, {"run_id": run_id, "host_id": db.host})


class _Closed:
    def __init__(self, *a, **k):
        pass

    def settimeout(self, *_):
        pass

    def connect(self, *_):
        raise OSError("closed")

    def close(self):
        pass


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr(socket, "socket", _Closed)
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
            if wid is None:
                c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_boss', 'BOSS') "
                               "ON CONFLICT DO NOTHING"))
                c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                               "('ws_deploy_truth', 'org_boss', 'ws') ON CONFLICT DO NOTHING"))
                wid = "ws_deploy_truth"
    except Exception as error:  # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    pid = f"p_{uuid.uuid4().hex[:10]}"
    host = f"dh_{pid}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'deploy truth test', :i)"), {"i": pid, "w": wid})
        c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, status, "
                       " port_min, port_max) VALUES (:h, :p, 'vm', :a, 'verified', 8000, 8999)"),
                  {"h": host, "p": pid, "a": ADDRESS})
    yield SimpleNamespace(engine=engine, project=pid, host=host)
    with engine.begin() as c:
        for table in ("stack_deployments", "deployment_runs", "code_task_runs",
                      "deployment_targets", "deployment_hosts", "events"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _target(c, db, app, port, *, repo="boss-sandbox", kind="ssh", status="active",
            manifest=None):
    tid = f"deptgt_{uuid.uuid4().hex[:12]}"
    c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                   " target_kind, target_ref, host_id, domains, created_by, status, "
                   " manifest_path, created_at) VALUES (:t, :p, :a, :r, 'staging', :k, "
                   " 'root@x:22', :h, CAST(:d AS JSONB), 'ceo', :s, :m, "
                   " clock_timestamp())"),
              {"t": tid, "p": db.project, "a": app, "r": repo, "k": kind, "h": db.host,
               "d": json.dumps([f"{ADDRESS}:{port}"]), "s": status, "m": manifest})
    return tid


def _sdep(c, db, target_id, status, *, steps=None, log="", reason=None, at=None):
    at = at or T0
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
        " commit_sha, status, steps, log_tail, failure_reason, created_by, created_at, "
        " finished_at, open_url) VALUES (:i, :p, :t, :h, 'acme/boss-sandbox', 'main', :c, :s, "
        " CAST(:steps AS JSONB), :log, :r, 'test', :at, :fin, :url)"),
        {"i": f"sdep_{uuid.uuid4().hex[:12]}", "p": db.project, "t": target_id, "h": db.host,
         "c": uuid.uuid4().hex + "abcdefgh", "s": status, "steps": json.dumps(steps or []),
         "log": log, "r": reason, "at": at,
         "fin": at if status in ("healthy", "failed") else None,
         "url": None})


def _build(c, pid, files, *, app):
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    spec = {"owner": "acme", "repo": "boss-sandbox", "instructions": "build it"}
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
                   " finalize_ctx, pull_request_number) VALUES (:i, :p, :t, 'completed', "
                   " CAST(:s AS JSONB), CAST(:st AS JSONB), CAST(:f AS JSONB), 7)"),
              {"i": run_id, "p": pid, "t": app, "s": json.dumps(spec),
               "st": json.dumps({"impl_files": files}), "f": json.dumps({"app_name": app})})
    return run_id


# ── a deploy that failed before it had a record ──────────────────────────────────────────────

def _failed_deploy_step(c, db, *, target_id=None, reason, at=None):
    """A workflow whose deploy step failed in source resolution — no stack deployment exists
    (helpdesk-laptop, wfr_e9d48d68: "… has no ref '47e8687…' in local_…", dead-lettered)."""
    at = at or T0
    did, vid, rid = (f"wfd_{uuid.uuid4().hex[:10]}", f"wfv_{uuid.uuid4().hex[:10]}",
                     f"wfr_{uuid.uuid4().hex[:10]}")
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   " created_by) "
                   "VALUES (:i, :p, :k, 'Ship', 'ceo')"), {"i": did, "p": db.project, "k": did})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                   " version, spec, content_hash, created_by) VALUES (:i, :p, :d, 1, "
                   " '{}'::jsonb, 'h', 'ceo')"), {"i": vid, "p": db.project, "d": did})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   " workflow_version_id, actor, status) VALUES (:i, :p, :d, :v, 'ceo', "
                   " 'deadlettered')"), {"i": rid, "p": db.project, "d": did, "v": vid})
    snapshot = {"capability": "deploy", **({"stack_gate_target_id": target_id} if target_id
                                           else {})}
    c.execute(text(
        "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
        " status, output, binding_snapshot, attempt, last_error, completed_at, updated_at) "
        "VALUES (:i, :p, :r, 'deploy', 'capability', 'deadlettered', CAST(:o AS JSONB), "
        " CAST(:b AS JSONB), 3, :e, :at, :at)"),
        {"i": f"wsr_{uuid.uuid4().hex[:10]}", "p": db.project, "r": rid, "at": at, "e": reason,
         "o": json.dumps({"capability": "deploy", "status": "failed", "error": reason}),
         "b": json.dumps(snapshot)})
    return rid


def _forget_workflows(c, db):
    for table in ("workflow_step_runs", "workflow_runs", "workflow_versions",
                  "workflow_definitions"):
        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": db.project})


def test_a_deploy_that_failed_before_its_record_says_last_deploy_failed_with_retry(db):
    from boss_engine.services.project_experience import deployments

    reason = ("The local repository local_x/app has no ref '47e8687'. Check the branch or "
              "commit.")
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Helpdesk", 8020, repo="local_x/app", kind="stack")
            rid = _failed_deploy_step(c, db, target_id=target, reason=reason)
            told = truth(c, deploy_truth.project_targets(c, db.project))[target]
            view = deployments.view(c, db.project)
        assert told["state"] == "failed" and told["word"] == "Last deploy failed", told
        assert told["reason"] == reason
        assert told["attempt"]["kind"] == "step" and told["attempt"]["step_id"] == "deploy"
        assert told["attempt"]["workflow_run_id"] == rid
        assert rid in told["attempt"]["run_href"]
        env = view["environments"][0]
        assert env["deploy"]["state"] == "failed" and env["deploy"]["reason"] == reason
        failed = [h for h in view["history"] if h["state"] == "failed"]
        assert failed and failed[0]["step_id"] == "deploy" and failed[0]["workflow_run_id"] == rid
        assert failed[0]["reason"].startswith("The local repository local_x/app has no ref")

        # Retried: the step is ready again, so it is no longer the newest failure.
        with db.engine.begin() as c:
            c.execute(text("UPDATE workflow_step_runs SET status = 'ready' WHERE "
                           "workflow_run_id = :r"), {"r": rid})
            again = truth(c, deploy_truth.project_targets(c, db.project))[target]
        assert again["state"] == "not_deployed"
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_a_first_deploy_that_failed_before_any_target_is_not_not_deployed_yet(db):
    from boss_engine.services.project_experience import deployments

    try:
        with db.engine.begin() as c:
            rid = _failed_deploy_step(c, db, reason="The build opened no change, so there is "
                                                    "no commit to deploy.")
            view = deployments.view(c, db.project)
        assert view["environments"] == []
        assert view["empty"]["label"] == "Last deploy failed" and view["empty"]["failed"]
        assert view["empty"]["workflow_run_id"] == rid and view["empty"]["step_id"] == "deploy"
        assert view["history"] and view["history"][0]["state"] == "failed"
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)


def test_a_newer_stack_deployment_outranks_an_older_failed_step(db):
    try:
        with db.engine.begin() as c:
            target = _target(c, db, "Helpdesk", 8021, kind="stack")
            _failed_deploy_step(c, db, target_id=target, reason="no ref", at=T0)
            _sdep(c, db, target, "healthy", at=T0 + timedelta(hours=1))
            told = truth(c, deploy_truth.project_targets(c, db.project))[target]
        assert told["state"] == "live", told
    finally:
        with db.engine.begin() as c:
            _forget_workflows(c, db)
