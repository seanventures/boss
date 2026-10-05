"""The activity log reads every stream, filters in SQL, and still tells the truth about each row.

Ported from the engine's in-memory contract tests (engine/tests/test_activity_log.py) when the
log became SQL: an in-memory double that ignored WHERE clauses could no longer say anything
about a log whose filters ARE the WHERE clauses. Same promises, against Postgres.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from boss_engine.services.activity_log import (
    SOURCES,
    ActivityLogService,
    _resolve_sources,
)
from boss_engine.services import activity_log as module

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT, utc

OTHER = "boss-log-other"
T_OLD, T_MID, T_NEW = utc(2026, 8, 14, 10), utc(2026, 8, 14, 11), utc(2026, 8, 14, 12)
ITEM_KEYS = {"id", "source", "kind", "status", "title", "actor", "entity_type", "entity_id",
             "stage", "created_at", "data", "level", "run_id", "workflow_run_id", "agent",
             "service", "body"}
SECRET_TOKEN = "tok_live_should_never_surface"
SECRET_EMAIL = "ops@example.com"
SECRET_AWS = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture
def log(engine):
    with engine.begin() as c:
        seed.wipe(c)
        c.execute(seed.text("INSERT INTO projects (id, workspace_id, name, slug) "
                            "SELECT :o, workspace_id, 'Other', :o FROM projects WHERE id = :p "
                            "ON CONFLICT (id) DO NOTHING"), {"o": OTHER, "p": PROJECT})
        seed.wipe(c, OTHER)
    yield engine
    with engine.begin() as c:
        seed.wipe(c)
        seed.wipe(c, OTHER)


def _list(engine, **params) -> list[dict]:
    with engine.begin() as c:
        return ActivityLogService.query(c, PROJECT, **params)["items"]


def _ids(items) -> list[str]:
    return [i["id"] for i in items]


def _timeline(engine) -> None:
    with engine.begin() as c:
        seed.event(c, id="evt_fail", event_type="job.failed", at=T_MID, entity_id="job_fail",
                   data={"reason": "sandbox exploded"})
        seed.event(c, id="evt_budget", event_type="budget.reserved", at=T_OLD,
                   entity_type="reservation", entity_id="rsv_1", data={"note": "reserved"})
        seed.event(c, id="evt_lease", event_type="job.leased", at=T_NEW, entity_id="job_wait")
        seed.event(c, id="evt_other", event_type="job.failed", at=T_NEW, project=OTHER)


def test_filter_by_status_returns_only_failed(log):
    _timeline(log)
    items = _list(log, status="failed")
    assert _ids(items) == ["evt_fail"]
    assert items[0]["title"] == "Job Failed" and items[0]["level"] == "error"
    assert ITEM_KEYS <= set(items[0])


def test_filter_by_q_matches_kind_or_title(log):
    _timeline(log)
    assert _ids(_list(log, q="budget")) == ["evt_budget"]


def test_newest_and_oldest_first(log):
    _timeline(log)
    assert _ids(_list(log)) == ["evt_lease", "evt_fail", "evt_budget"]
    assert _ids(_list(log, order="asc")) == ["evt_budget", "evt_fail", "evt_lease"]


def test_a_non_time_sort_is_refused_with_its_reason(log):
    with log.begin() as c, pytest.raises(ValueError, match="Sort by time"):
        ActivityLogService.query(c, PROJECT, sort="kind")


def test_redacts_secret_material(log):
    with log.begin() as c:
        seed.event(c, id="evt_secret", event_type="job.failed", at=T_MID, data={
            "token": SECRET_TOKEN, "secret": "super-secret", "password": "hunter2",
            "api_key": SECRET_AWS, "authorization": "Bearer 0123456789abcdef0123",
            "note": f"ping {SECRET_EMAIL}", "aws_key": SECRET_AWS})
    blob = json.dumps(_list(log), default=str)
    for secret in (SECRET_TOKEN, SECRET_EMAIL, SECRET_AWS, "hunter2",
                   "Bearer 0123456789abcdef0123", "super-secret"):
        assert secret not in blob


def test_tenant_scope_excludes_other_project_rows(log):
    _timeline(log)
    assert set(_ids(_list(log))) == {"evt_budget", "evt_fail", "evt_lease"}


def test_does_not_invent_success_and_empty_payload_stays_empty(log):
    with log.begin() as c:
        seed.event(c, id="evt_info", event_type="budget.reserved", at=T_OLD)
    item = _list(log)[0]
    assert item["status"] == "info" and item["data"] == {}


def test_a_missing_table_skips_only_its_stream(log, monkeypatch):
    """A partial schema (an optional table absent) must not empty the whole log."""
    _timeline(log)
    monkeypatch.setattr(module, "_missing_sources", lambda _c: {"job", "receipt"})
    assert set(_ids(_list(log))) == {"evt_budget", "evt_fail", "evt_lease"}


def test_workflow_steps_are_a_source_with_duration_and_attempt(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, id="wsr_ok", workflow_run_id=wfr, attempt=0, created_at=T_OLD,
                  started_at=T_OLD, completed_at=T_MID)
    item = _list(log, source="workflow_step")[0]
    assert item["id"] == "wsr_ok" and item["status"] == "ok"
    assert item["workflow_run_id"] == wfr
    assert item["data"]["duration_seconds"] == 3600.0 and item["data"]["attempt"] == 0


def test_a_step_that_never_finished_has_no_duration_rather_than_zero(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, workflow_run_id=wfr, status="running", created_at=T_OLD, started_at=T_MID)
    assert _list(log, source="workflow_step")[0]["data"]["duration_seconds"] is None


def test_a_failed_step_carries_its_error_as_the_body(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, workflow_run_id=wfr, status="failed", attempt=2, created_at=T_OLD,
                  last_error="pytest exited 1: 3 failed")
    item = _list(log, source="workflow_step")[0]
    assert item["status"] == "failed" and item["level"] == "error"
    assert "pytest exited 1" in item["body"]


def test_a_retry_speaks_louder_than_a_clean_run_and_a_first_attempt_is_not_one(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, id="wsr_first", workflow_run_id=wfr, step_id="a", attempt=1,
                  created_at=T_OLD, completed_at=T_MID)
        seed.step(c, id="wsr_retried", workflow_run_id=wfr, step_id="b", attempt=2,
                  last_error="timed out, retried", created_at=T_OLD, completed_at=T_MID)
    items = {i["id"]: i for i in _list(log, source="workflow_step")}
    assert items["wsr_first"]["level"] == "info"
    assert items["wsr_retried"]["level"] == "warn"


def test_a_step_row_is_dated_when_it_ran_not_when_it_was_planned(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, id="wsr_late", workflow_run_id=wfr, step_id="a", created_at=T_OLD,
                  started_at=T_MID, completed_at=T_NEW)
        seed.step(c, id="wsr_running", workflow_run_id=wfr, step_id="b", status="running",
                  created_at=T_OLD, started_at=T_MID)
    items = {i["id"]: i for i in _list(log, source="workflow_step")}
    assert items["wsr_late"]["created_at"] == T_NEW.isoformat()
    assert items["wsr_running"]["created_at"] == T_MID.isoformat()


def test_deployments_are_a_source_and_a_failed_one_is_an_error(log):
    with log.begin() as c:
        seed.deployment(c, id="dpr_ok", at=T_MID, started_at=T_OLD, finished_at=T_MID)
        seed.deployment(c, id="dpr_bad", at=T_NEW, status="failed",
                        failure_reason="health check never passed")
    items = {i["id"]: i for i in _list(log, source="deployment")}
    assert items["dpr_ok"]["status"] == "ok" and items["dpr_ok"]["data"]["app"] == "checkout"
    # An app is not a process: PROCESS is API and worker (D16) — and the old `service=` link
    # to an app's deploys still finds them.
    assert items["dpr_ok"]["service"] == ""
    with log.begin() as c:
        facets = ActivityLogService.facets(c, PROJECT)
        by_app = ActivityLogService.query(c, PROJECT, service="checkout")
    assert "checkout" not in facets["services"]
    assert {i["id"] for i in by_app["items"]} == {"dpr_ok", "dpr_bad"}
    assert items["dpr_ok"]["data"]["duration_seconds"] == 3600.0
    assert items["dpr_bad"]["level"] == "error"
    assert "health check never passed" in items["dpr_bad"]["body"]


def test_platform_is_the_infrastructure_and_queue_is_the_queue(log):
    assert set(module.QUEUE_SOURCES) == {"job", "deadletter", "receipt"}
    assert set(module.INFRA_SOURCES) == set(module.QUEUE_SOURCES)
    with log.begin() as c:
        seed.deployment(c, id="dpr_ok", at=T_MID)
        seed.job(c, id="job_1", at=T_OLD, status="queued")
    assert _ids(_list(log, source="platform")) == ["dpr_ok"]


def test_workflow_run_scope_narrows_to_one_run_across_sources(log):
    with log.begin() as c:
        mine, theirs = seed.workflow(c), seed.workflow(c)
        seed.step(c, id="wsr_mine", workflow_run_id=mine, created_at=T_OLD)
        seed.step(c, id="wsr_theirs", workflow_run_id=theirs, created_at=T_OLD)
        seed.event(c, id="evt_mine", event_type="workflow.run_started", at=T_NEW,
                   entity_type="workflow_run", entity_id=mine)
        seed.event(c, id="evt_theirs", event_type="workflow.run_started", at=T_NEW,
                   entity_type="workflow_run", entity_id=theirs)
    assert set(_ids(_list(log, workflow_run_id=mine))) == {"wsr_mine", "evt_mine"}
    assert _list(log, run_id=mine) == [], "a workflow run is not a work task"


def test_several_sources_groups_and_repeats(log):
    with log.begin() as c:
        wfr = seed.workflow(c)
        seed.step(c, id="wsr_1", workflow_run_id=wfr, created_at=T_OLD)
        seed.deployment(c, id="dpr_1", at=T_OLD)
        seed.job(c, id="job_1", at=T_OLD, status="queued")
        seed.receipt(c, id="rcp_1", at=T_OLD)
    assert set(_ids(_list(log, source="workflow_step,deployment"))) == {"wsr_1", "dpr_1"}
    assert set(_ids(_list(log, source="workflow_step,queue"))) == {"wsr_1", "job_1", "rcp_1"}
    assert set(_ids(_list(log, exclude_source="deployment,queue"))) == {"wsr_1"}
    assert _resolve_sources(" workflow_step , workflow_step ,, deployment ") == [
        "workflow_step", "deployment"]
    assert _resolve_sources(",".join(SOURCES)) == list(SOURCES)
    with log.begin() as c, pytest.raises(ValueError, match="nonsense"):
        ActivityLogService.query(c, PROJECT, source="workflow_step,nonsense")


def _step_log(engine) -> str:
    with engine.begin() as c:
        wfr, elsewhere = seed.workflow(c), seed.workflow(c)
        seed.step(c, id="wsr_build", workflow_run_id=wfr, step_id="build", created_at=T_OLD,
                  output={"code_task_run_id": "crun_build1"})
        seed.step(c, id="wsr_preview", workflow_run_id=wfr, step_id="preview", created_at=T_OLD,
                  output={"run_id": "crun_other"})
        seed.step(c, id="wsr_build_elsewhere", workflow_run_id=elsewhere, step_id="build",
                  created_at=T_OLD)
        seed.event(c, id="evt_build_ready", event_type="workflow.step_ready", at=T_NEW,
                   entity_type="workflow_run", entity_id=wfr, data={"step_id": "build"})
        seed.event(c, id="evt_preview_ready", event_type="workflow.step_ready", at=T_NEW,
                   entity_type="workflow_run", entity_id=wfr, data={"step_id": "preview"})
        seed.event(c, id="evt_build_work", event_type="code_task.stage_completed", at=T_NEW,
                   entity_type="code_task_run", entity_id="crun_build1")
        seed.event(c, id="evt_other_work", event_type="code_task.stage_completed", at=T_NEW,
                   entity_type="code_task_run", entity_id="crun_other")
        seed.turn(c, id="turn_build", at=T_NEW, run_id="crun_build1", level="error",
                  content="tests failed")
    return wfr


def test_a_step_scope_is_the_steps_own_rows_and_the_work_it_started(log):
    wfr = _step_log(log)
    assert set(_ids(_list(log, workflow_run_id=wfr, step_id="build"))) == {
        "wsr_build", "evt_build_ready", "evt_build_work", "turn_build"}
    with log.begin() as c, pytest.raises(ValueError):
        ActivityLogService.query(c, PROJECT, step_id="build")


def test_the_step_scope_counts_what_it_lists(log):
    wfr = _step_log(log)
    with log.begin() as c:
        facets = ActivityLogService.facets(c, PROJECT, workflow_run_id=wfr, step_id="build")
    assert facets["sources"]["workflow_step"] == 1
    assert facets["sources"]["event"] == 2
    assert facets["sources"]["agent_turn"] == 1
    assert facets["total"] == 4


def test_a_page_names_its_runs_and_steps(log):
    """Rows carry ids; the response carries their names, so the console never shows an id."""
    with log.begin() as c:
        wfr = seed.workflow(c, subject="Lead Triage Inbox")
        crun = seed.build(c, title="Ship it #182")
        seed.event(c, event_type="workflow.step_ready", at=T_NEW, entity_type="workflow_run",
                   entity_id=wfr, data={"step_id": "plan"})
        seed.llm_call(c, at=T_NEW, run_id=crun)
    with log.begin() as c:
        page = ActivityLogService.query(c, PROJECT)
    assert page["labels"][crun] == "Ship it #182"
    assert page["labels"][wfr] == "Lead Triage Inbox"
    assert page["labels"][f"{wfr}/plan"] == "Plan it"


def test_hide_verbose_and_negation_are_server_side(log):
    with log.begin() as c:
        seed.turn(c, id="t_verbose", at=T_OLD, level="verbose")
        seed.turn(c, id="t_error", at=T_MID, level="error", role="adversarial_reviewer")
        seed.llm_call(c, id="l_ok", at=T_NEW, role="software_engineer")
    assert set(_ids(_list(log, hide_verbose="1"))) == {"t_error", "l_ok"}
    assert set(_ids(_list(log, exclude_level="error"))) == {"t_verbose", "l_ok"}
    assert set(_ids(_list(log, agent="adversarial_reviewer"))) == {"t_error"}
    assert set(_ids(_list(log, exclude_agent="adversarial_reviewer"))) == {"t_verbose", "l_ok"}
    assert set(_ids(_list(log, level="error,info"))) == {"t_error", "l_ok"}


def test_an_act_scope_is_that_works_turns_calls_and_events(log):
    with log.begin() as c:
        seed.turn(c, id="t_mine", at=T_OLD, act_id="act_one")
        seed.turn(c, id="t_theirs", at=T_OLD, act_id="act_two")
        seed.llm_call(c, id="l_mine", at=T_MID, act_id="act_one")
        seed.event(c, id="e_mine", event_type="act.completed", at=T_NEW,
                   entity_type="agent_act", entity_id="act_one")
        seed.app_log(c, id="a_none", at=T_NEW)
    assert set(_ids(_list(log, act="act_one"))) == {"t_mine", "l_mine", "e_mine"}


def test_subject_is_the_thing_another_screen_links_about(log):
    """Infra and Observability link `source=deployment&subject=<target>`; this is that read."""
    with log.begin() as c:
        seed.deployment(c, id="dpr_a", at=T_OLD, target_id=None)
        seed.deployment(c, id="dpr_b", at=T_MID, app="billing")
        seed.llm_call(c, id="l_opus", at=T_NEW, model="claude-opus")
        seed.llm_call(c, id="l_sonnet", at=T_NEW, model="claude-sonnet")
    assert _ids(_list(log, source="deployment", subject="billing")) == ["dpr_b"]
    assert _ids(_list(log, source="llm_call", subject="claude-opus")) == ["l_opus"]


def test_a_relative_range_is_a_window_and_the_response_says_which(log):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with log.begin() as c:
        seed.event(c, id="e_recent", event_type="x.recent", at=now - timedelta(minutes=5))
        seed.event(c, id="e_old", event_type="x.old", at=now - timedelta(hours=3))
    with log.begin() as c:
        page = ActivityLogService.query(c, PROJECT, range="1h")
    assert _ids(page["items"]) == ["e_recent"]
    assert page["window"]["range"] == "1h" and page["window"]["from"]
