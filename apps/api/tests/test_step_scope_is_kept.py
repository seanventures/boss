"""A step's log (`wfr` + `step`) is kept, and the step panel reads it in one bounded query."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT


@pytest.fixture
def step_log(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        seed.wipe(c)
        wfr = seed.workflow(c, subject="Lead Triage Inbox")
        crun = seed.build(c, title="Ship it #182")
        seed.step(c, id="wsr_build", workflow_run_id=wfr, step_id="build",
                  created_at=now - timedelta(minutes=30), started_at=now - timedelta(minutes=29),
                  status="running", output={"code_task_run_id": crun})
        seed.event(c, id="evt_ready", event_type="workflow.step_ready",
                   at=now - timedelta(minutes=29), entity_type="workflow_run", entity_id=wfr,
                   data={"step_id": "build"})
        for i in range(8):
            seed.turn(c, id=f"turn_{i}", at=now - timedelta(minutes=20 - i), run_id=crun,
                      level=["verbose", "info", "warn", "error"][i % 4])
        seed.llm_call(c, id="llm_ok", at=now - timedelta(minutes=5), run_id=crun)
        seed.turn(c, id="turn_other", at=now, run_id="crun_somewhere_else", level="error")
    yield wfr, crun
    with engine.begin() as c:
        seed.wipe(c)


def test_the_step_scope_is_its_rows_and_its_builds_rows(client, step_log):
    wfr, crun = step_log
    body = client.get("/v1/activity", params={"project_id": PROJECT, "workflow_run_id": wfr,
                                              "step_id": "build"}).json()
    ids = {i["id"] for i in body["items"]}
    assert ids == {"wsr_build", "evt_ready", "llm_ok", *(f"turn_{i}" for i in range(8))}
    assert body["labels"][wfr] == "Lead Triage Inbox"
    assert body["labels"][f"{wfr}/build"] == "Build it"
    assert body["labels"][crun] == "Ship it #182"


def test_summary_is_five_non_verbose_rows_errors_first_with_counts(client, step_log):
    wfr, _crun = step_log
    body = client.get("/v1/activity", params={"project_id": PROJECT, "workflow_run_id": wfr,
                                              "step_id": "build", "summary": "1",
                                              "limit": 5}).json()
    levels = [i["level"] for i in body["items"]]
    assert len(levels) == 5 and "verbose" not in levels
    assert levels[:2] == ["error", "error"]
    assert body["counts"] == {"error": 2, "warn": 2}


def test_a_step_without_its_run_is_refused(client):
    response = client.get("/v1/activity", params={"project_id": PROJECT, "step_id": "build"})
    assert response.status_code == 422
    assert "step_id needs workflow_run_id" in response.json()["detail"]
