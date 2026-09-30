"""No-spend simulated fixture for POST /v1/code-tasks.

The governed factory has no deterministic_brief / dry-run worker that can
complete a real code-task without a model. simulated=true persist a labeled
run and must never enqueue a job, reserve budget, or open a PR.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.code_task_service import (
    is_simulated_run,
    retry_code_task,
    start_code_task,
    start_followup_task,
)
from apps.api.app.services.jobs import JobLeaseService
from apps.api.app.config import get_settings


PID = "boss-dogfood"


class _CountingJobs:
    def __init__(self):
        self.settings = get_settings()
        self.created: list[dict] = []

    def create_job(self, *_a, **kwargs):
        self.created.append(kwargs)
        return {"id": "job_should_not_exist"}


def test_start_code_task_simulated_does_not_enqueue_job(engine):
    jobs = _CountingJobs()
    with begin(engine) as c:
        run_id = start_code_task(
            c,
            project_id=PID,
            task_id=None,
            title="fixture probe",
            instructions="no model",
            acceptance_criteria=["readable"],
            test_commands=[],
            owner="_fixture",
            repo="_fixture",
            base_branch="main",
            finalize_ctx=None,
            jobs=jobs,
            simulated=True,
        )
        row = c.execute(
            text(
                "SELECT status, phase, spec, state, usage, reservation_id "
                "FROM code_task_runs WHERE id = :id"
            ),
            {"id": run_id},
        ).mappings().one()
        job_count = c.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :id"),
            {"id": run_id},
        ).scalar_one()

    spec = row["spec"] if isinstance(row["spec"], dict) else json.loads(row["spec"])
    state = row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])
    usage = row["usage"] if isinstance(row["usage"], dict) else json.loads(row["usage"])
    assert row["status"] == "simulated"
    assert row["phase"] == "fixture"
    assert spec["simulated"] is True
    assert spec.get("open_pr") is False
    assert state["evidence"]["llm_called"] is False
    assert state["evidence"]["job_enqueued"] is False
    assert int(usage.get("cost_usd_micros") or 0) == 0
    assert row["reservation_id"] is None
    assert jobs.created == []
    assert int(job_count) == 0
    assert is_simulated_run(
        {"status": row["status"], "spec": spec}
    )


def test_start_code_task_simulated_rejects_open_pr(engine):
    jobs = _CountingJobs()
    with begin(engine) as c:
        with pytest.raises(ValueError, match="cannot open a PR"):
            start_code_task(
                c,
                project_id=PID,
                task_id=None,
                title="bad fixture",
                instructions="no",
                acceptance_criteria=[],
                test_commands=[],
                owner="_fixture",
                repo="_fixture",
                base_branch="main",
                finalize_ctx=None,
                jobs=jobs,
                open_pr=True,
                simulated=True,
            )
    assert jobs.created == []


def test_retry_and_followup_fail_closed_on_simulated(engine):
    jobs = _CountingJobs()
    settings = get_settings()
    real_jobs = JobLeaseService(settings)
    with begin(engine) as c:
        run_id = start_code_task(
            c,
            project_id=PID,
            task_id=None,
            title="fixture no retry",
            instructions="no",
            acceptance_criteria=[],
            test_commands=[],
            owner="_fixture",
            repo="_fixture",
            base_branch="main",
            finalize_ctx=None,
            jobs=jobs,
            simulated=True,
        )
        with pytest.raises(ValueError, match="follow up a simulated"):
            start_followup_task(
                c, parent_run_id=run_id, instructions="change it", jobs=real_jobs
            )
    with pytest.raises(ValueError, match="retry a simulated"):
        retry_code_task(engine, settings, real_jobs, run_id)
    assert jobs.created == []


def test_api_post_simulated_fixture_round_trip(client, engine):
    created = client.post(
        "/v1/code-tasks",
        json={
            "project_id": PID,
            "title": "api fixture",
            "instructions": "no spend",
            "simulated": True,
            "open_pr": False,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "simulated"
    assert body["simulated"] is True
    assert body["job_enqueued"] is False
    run_id = body["run_id"]
    assert run_id

    fetched = client.get(f"/v1/code-tasks/{run_id}")
    assert fetched.status_code == 200, fetched.text
    run = fetched.json()
    assert run["status"] == "simulated"
    assert run["phase"] == "fixture"
    assert run["spec"]["simulated"] is True
    assert int((run.get("usage") or {}).get("cost_usd_micros") or 0) == 0

    wip = client.get("/v1/factory/wip", params={"project_id": PID, "wip_limit": 5})
    assert wip.status_code == 200, wip.text
    assert wip.json().get("fixture_supported") is True

    with begin(engine) as c:
        job_count = c.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'run_id' = :id"),
            {"id": run_id},
        ).scalar_one()
    assert int(job_count) == 0


def test_api_simulated_plus_open_pr_is_422(client):
    response = client.post(
        "/v1/code-tasks",
        json={
            "project_id": PID,
            "title": "bad",
            "instructions": "no",
            "simulated": True,
            "open_pr": True,
        },
    )
    assert response.status_code == 422, response.text
    assert "cannot open a PR" in response.text
