"""A correct diagnosis you cannot act on is still a dead end.

Asked why a panel step had failed, BOSS traced it properly — the meeting job returned 500 and
deadlettered, cleanup swept the run as "abandoned", and the root cause was meetings reading the
wrong model setting. Then it finished with "tell me to go ahead and I will run it", and offered
no button.

    "the panel retry endpoint currently has no exposed BOSS tool"

Its own words, and exactly right. The console has had "Try … again" the whole time; the agent
had no tool behind it, so the one action that follows the diagnosis was unreachable. This is
that operation, reachable by the agent, with the same guards the endpoint applies.

Run against a database, because every guard here is a status check on a real row.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.tools import build_default_registry


def _seed(engine, project_id, *, status="deadlettered", run_status="deadlettered"):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
            "VALUES ('wfd_r1', :p, 'standard_delivery', 'Standard Delivery', 'ceo') "
            "ON CONFLICT (id) DO NOTHING"), {"p": project_id})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            "        status, spec, content_hash, created_by) "
            "VALUES ('wfv_r1', :p, 'wfd_r1', 1, 'published', CAST('{\"steps\":[]}' AS JSONB), "
            "        'h1', 'ceo') ON CONFLICT (id) DO NOTHING"), {"p": project_id})
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            "        workflow_version_id, actor, status, error) "
            "VALUES ('wfr_r1', :p, 'wfd_r1', 'wfv_r1', 'ceo', :s, 'it all went wrong') "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error"),
            {"p": project_id, "s": run_status})
        c.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
            "        step_kind, status, attempt, max_attempts, last_error) "
            "VALUES ('wsr_r1', :p, 'wfr_r1', 'meeting', 'capability', :s, 3, 3, 'cancelled') "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, attempt = 3"),
            {"p": project_id, "s": status})


def _retry(engine, project_id, step_id="meeting", run_id="wfr_r1"):
    reg = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    return reg.invoke("retry_workflow_step", ctx, project_id=project_id, actor="ceo",
                      args={"run_id": run_id, "step_id": step_id, "project_id": project_id})


def test_a_dead_step_goes_back_in_the_queue(engine):
    pid = get_settings().default_project_id
    _seed(engine, pid)
    result = _retry(engine, pid)
    assert result["ok"] is True and result["status"] == "ready"

    with engine.begin() as c:
        row = c.execute(text(
            "SELECT status, attempt, last_error FROM workflow_step_runs WHERE id='wsr_r1'"
        ).execution_options()).mappings().first()
    assert row["status"] == "ready"
    # The attempt counter resets, or a step at 3/3 is re-queued straight back into dead-letter.
    assert row["attempt"] == 0
    assert row["last_error"] is None


def test_the_run_is_revived_with_it(engine):
    """A re-queued step sitting under a dead run never gets picked up."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    _retry(engine, pid)
    with engine.begin() as c:
        run = c.execute(text(
            "SELECT status, error FROM workflow_runs WHERE id='wfr_r1'")).mappings().first()
    assert run["status"] == "running"
    assert run["error"] is None


def test_a_live_step_is_refused(engine):
    """Re-queueing something already running would run it twice."""
    pid = get_settings().default_project_id
    _seed(engine, pid, status="running", run_status="running")
    result = _retry(engine, pid)
    assert result["ok"] is False
    assert "nothing to retry" in result["error"]


def test_an_unknown_step_names_the_steps_that_exist(engine):
    """A step is addressed by ID ('meeting'); the console shows its LABEL ('Convene a
    panel'), so the obvious guess is the wrong one. "Not found" would send the reader hunting
    for a typo they did not make."""
    pid = get_settings().default_project_id
    _seed(engine, pid)
    result = _retry(engine, pid, step_id="Convene a panel")
    assert result["ok"] is False
    assert [s["step_id"] for s in result["steps_on_this_run"]] == ["meeting"]


def test_it_is_a_write_the_operator_confirms(engine):
    """It changes run state, so it must be offered as a button rather than taken silently."""
    reg = build_default_registry()
    spec = reg._tools["retry_workflow_step"]
    assert spec.kind == "write"
    assert spec.reversibility == "safe"
