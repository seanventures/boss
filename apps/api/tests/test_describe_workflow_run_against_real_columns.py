"""The run-status tool, executed — not grepped.

`describe_workflow_run` shipped selecting `error` from `workflow_step_runs`, which has
`last_error`. Every source-level test passed: the tool existed, was registered as a read, and
its body contained every string those tests asserted on. It failed the first time a person
used it, with `column "error" does not exist` — the exact failure mode the same commit added
schema hints to rescue the agent from.

A tool that touches the database has to be RUN against one. So this executes it end to end and
asserts on the shape it returns, which is the only thing that can catch a column name.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.tools import build_discuss_read_registry


def _seed_failed_run(engine, project_id: str) -> str:
    """A run that deadlettered because a declared input was never supplied."""
    spec = {
        "entry_step": "edit",
        "input_schema": {"media_artifact_id": "string", "title": "string"},
        "steps": [{"id": "edit", "kind": "capability", "next": [],
                   "binding": {"capability": "video_edit",
                               "inputs": {"media_artifact_id": {"from_input":
                                                                "media_artifact_id"}}}}],
    }
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
            "VALUES ('wfd_t1', :p, 'family_channel_video', 'Family channel video', 'ceo') "
            "ON CONFLICT (id) DO NOTHING"), {"p": project_id})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "                               status, spec, content_hash, created_by) "
            "VALUES ('wfv_t1', :p, 'wfd_t1', 1, 'published', CAST(:s AS JSONB), :h, 'ceo') "
            "ON CONFLICT (id) DO NOTHING"),
            {"p": project_id, "s": json.dumps(spec),
             "h": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()})
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "        workflow_version_id, actor, status, input, error) "
            "VALUES ('wfr_t1', :p, 'wfd_t1', 'wfv_t1', 'ceo', 'deadlettered', "
            "        CAST('{}' AS JSONB), :e) ON CONFLICT (id) DO NOTHING"),
            {"p": project_id,
             "e": "this workflow was started without 'media_artifact_id'"})
        c.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
            "        step_kind, status, attempt, max_attempts, last_error) "
            "VALUES ('wsr_t1', :p, 'wfr_t1', 'edit', 'capability', 'deadlettered', 3, 3, :e) "
            "ON CONFLICT (id) DO NOTHING"),
            {"p": project_id, "e": "needs 'media_artifact_id', which is not set"})
    return "wfr_t1"


def _call(engine, run_id: str, project_id: str):
    registry = build_discuss_read_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    return registry.invoke("describe_workflow_run", ctx, project_id=project_id, actor="ceo",
                           args={"run_id": run_id, "project_id": project_id})


def test_it_runs_at_all(engine):
    """THE TEST THAT WAS MISSING. Every source assertion passed while this raised."""
    project_id = get_settings().default_project_id
    run_id = _seed_failed_run(engine, project_id)
    result = _call(engine, run_id, project_id)
    assert result.get("ok") is True, result


def test_it_reports_the_step_error_from_the_column_that_holds_it(engine):
    """workflow_runs.error and workflow_step_runs.last_error are different names."""
    project_id = get_settings().default_project_id
    run_id = _seed_failed_run(engine, project_id)
    result = _call(engine, run_id, project_id)
    step = result["steps"][0]
    assert step["last_error"] == "needs 'media_artifact_id', which is not set"
    assert step["attempt"] == 3 and step["max_attempts"] == 3


def test_it_names_the_missing_input_and_the_way_out(engine):
    """"it is erroring out but there's no option or solution given to the user"."""
    project_id = get_settings().default_project_id
    run_id = _seed_failed_run(engine, project_id)
    result = _call(engine, run_id, project_id)

    # A SET: `jsonb` does not preserve key order, so asserting a list here would be a test
    # that fails on Postgres's storage decisions rather than on the behaviour.
    assert set(result["declared_inputs"]) == {"media_artifact_id", "title"}
    assert result["supplied_inputs"] == []
    assert set(result["missing_inputs"]) == {"media_artifact_id", "title"}

    remedy = result["remedy"]
    assert "media_artifact_id" in remedy["diagnosis"]
    # A concrete next action, naming the tool and the inputs — not "the run failed".
    assert "start_workflow" in remedy["do_this"]
    assert set(remedy["needs_from_operator"]) == {"media_artifact_id", "title"}
    # And the thing that must NOT happen: editing a dead run's row to make it look alive.
    assert "workflow_runs" in remedy["do_not"]


def test_a_code_task_run_is_refused_by_name(engine):
    project_id = get_settings().default_project_id
    result = _call(engine, "crun_abc123", project_id)
    assert result["ok"] is False
    assert "diagnose_run" in result["error"]
