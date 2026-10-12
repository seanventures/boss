"""A deploy's "Open full log" shows the deploy: every step it ran, and the records it is made of.

The Infra deploy drawer linked `?area=activity&src=deployment&subject=delivery:deprun_…` and the
log said "0 lines in 24h" for a deploy that had run five steps. Three things were wrong:
the subject was a deploy REF (`<kind>:<id>`) compared whole to row ids; the deploy stream had one
row per deploy, so its steps (label, status, log) never reached the log; and a stack deploy
(boss.deploy.yaml) was not a stream at all.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from boss_engine.services.activity_log import ActivityLogService

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT, utc

T0 = utc(2026, 8, 14, 10)
SECRET = "tok_live_should_never_surface"


@pytest.fixture
def log(engine):
    with engine.begin() as c:
        seed.wipe(c)
    yield engine
    with engine.begin() as c:
        seed.wipe(c)


def _list(engine, **params) -> list[dict]:
    with engine.begin() as c:
        return ActivityLogService.query(c, PROJECT, order="asc", limit=100, **params)["items"]


def _at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _host_deploy(c) -> str:
    run = seed.deployment(c, id="deprun_full", at=T0, app="helpdesk", environment="staging",
                          started_at=T0 + timedelta(minutes=1),
                          finished_at=T0 + timedelta(minutes=4))
    steps = [
        {"step": "resolve", "label": "Resolve source", "status": "ok", "log": "repo=acme/app",
         "started_at": _at(1), "ended_at": _at(1)},
        {"step": "preflight", "label": "Preflight checks", "status": "ok", "log": "ready",
         "started_at": _at(2), "ended_at": _at(2)},
        {"step": "transfer", "label": "Build, transfer and release", "status": "failed",
         "log": f"docker push refused; token {SECRET}", "error": "docker push refused",
         "started_at": _at(3), "ended_at": _at(3)},
        {"step": "observe", "label": "Health check", "status": "pending"},
    ]
    c.execute(text("UPDATE deployment_runs SET steps = CAST(:s AS JSONB), job_id = 'job_dep', "
                   "release_receipt_ids = CAST(:r AS JSONB) WHERE id = :i"),
              {"s": json.dumps(steps), "r": json.dumps(["rrcpt_dep"]), "i": run})
    seed.job(c, id="job_dep", at=T0 + timedelta(seconds=90), kind="deployment.host_apply")
    seed.event(c, id="e_planned", event_type="deployment_run.planned", at=T0,
               entity_type="deployment_run", entity_id=run)
    seed.event(c, id="e_job", event_type="job.leased", at=T0 + timedelta(seconds=95),
               entity_type="job", entity_id="job_dep")
    seed.event(c, id="e_receipt", event_type="resource_receipt.failed",
               at=T0 + timedelta(minutes=4), entity_type="resource_receipt",
               entity_id="rrcpt_dep")
    seed.event(c, id="e_other", event_type="job.leased", at=T0 + timedelta(minutes=2),
               entity_type="job", entity_id="job_someone_else")
    return run


def test_the_drawers_ref_finds_the_deploy_and_every_step_in_order(log):
    with log.begin() as c:
        run = _host_deploy(c)
    items = _list(log, source="deployment", subject=f"delivery:{run}")
    assert [i["id"] for i in items] == [run, f"{run}/1", f"{run}/2", f"{run}/3", f"{run}/4"]
    steps = {i["data"]["step"]: i for i in items if i["kind"] == "deploy.step"}
    assert steps["Preflight checks"]["body"] == "ready"
    assert steps["Build, transfer and release"]["level"] == "error"
    assert steps["Health check"]["level"] == "verbose"       # planned, never ran
    # Every line belongs to the deploy: its drawer and "By run" read the same id.
    assert {i["entity_id"] for i in items} == {run}
    # The bare id still answers, and a step's log is searchable.
    assert len(_list(log, source="deployment", subject=run)) == 5
    assert [i["id"] for i in _list(log, source="deployment", subject=run, q="push refused")] \
        == [f"{run}/3"]
    with log.begin() as c:
        facets = ActivityLogService.facets(c, PROJECT, source="deployment",
                                           subject=f"delivery:{run}")
    # "By run" groups a deploy's lines under the deploy.
    assert {"id": run, "n": 4, "label": ""} in facets["runs"] or \
        any(r["id"] == run and r["n"] >= 4 for r in facets["runs"]), facets["runs"]


def test_a_step_log_is_redacted(log):
    with log.begin() as c:
        run = _host_deploy(c)
    blob = json.dumps(_list(log, source="deployment", subject=run))
    assert SECRET not in blob


def test_the_whole_story_brings_the_records_the_deploy_is_made_of(log):
    """No stream filter: the deploy's lines plus its job, its receipt and its own events —
    and nothing about someone else's job."""
    with log.begin() as c:
        run = _host_deploy(c)
    ids = {i["id"] for i in _list(log, subject=f"delivery:{run}")}
    assert {"e_planned", "e_job", "e_receipt", "job_dep", run, f"{run}/3"} <= ids
    assert "e_other" not in ids


def test_a_stack_deploy_is_in_the_deploy_stream_with_its_steps(log):
    with log.begin() as c:
        seed.deployment(c, id="deprun_seed", at=T0, app="helpdesk", environment="staging")
        target = c.execute(text("SELECT deployment_target_id FROM deployment_runs "
                                "WHERE id = 'deprun_seed'")).scalar_one()
        steps = [{"name": "prepare", "status": "ok", "started_at": _at(1), "ended_at": _at(1)},
                 {"name": "health", "status": "failed", "started_at": _at(2),
                  "ended_at": _at(3)},
                 {"name": "finalize", "status": "pending", "started_at": None,
                  "ended_at": None}]
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            "commit_sha, manifest, manifest_text, status, steps, log_tail, preflight, "
            "created_by, created_at, updated_at, failure_reason) VALUES ('sdep_full', :p, :t, "
            "'acme/app', 'main', 'abc1234', '{}'::jsonb, '', 'failed', CAST(:s AS JSONB), "
            ":log, '{}'::jsonb, 'ceo', :at, :up, 'health check never passed')"),
            {"p": PROJECT, "t": target, "s": json.dumps(steps), "at": T0,
             "up": T0 + timedelta(minutes=5),
             "log": "$ [prepare]\nBOSS_PREPARED\n$ [health]\n/readyz answered 404\n"})
    items = _list(log, source="deployment", subject="stack:sdep_full")
    assert [i["id"] for i in items] == ["sdep_full", "sdep_full/1", "sdep_full/2",
                                        "sdep_full/3"]
    assert items[0]["level"] == "error" and "/readyz answered 404" in items[0]["body"]
    assert items[0]["data"]["app"] == "helpdesk"
    assert items[2]["data"]["step"] == "health" and items[2]["level"] == "error"
