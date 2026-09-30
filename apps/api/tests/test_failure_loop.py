"""Release-blocker fixes for the failure loop: swallowed errors surfaced, substrate fallback on
retry after a micro-VM infra failure, and a circuit breaker against blind identical retries."""
import importlib.util
import json

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.services.code_task_service import retry_code_task, start_code_task
from apps.api.app.services.core import make_id
from apps.api.app.services.governed_code_task_service import _resolve_use_microvm
from apps.api.app.services.jobs import JobLeaseService

PID = "boss-dogfood"
JOBS = JobLeaseService(get_settings())


def _insert_failed_run(connection, *, failure_class="infra", substrate="microvm",
                       attempt=1, error="claude agent failed:"):
    run_id = make_id("crun")
    fctx = {"failure_class": failure_class, "execution_substrate": substrate,
            "attempt": attempt, "reason_code": "pipeline_exception", "root_run_id": run_id}
    connection.execute(
        text("INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
             "finalize_ctx, error) VALUES (:id, :p, NULL, 'failed', 'done', 'Web app', "
             "CAST(:spec AS JSONB), CAST(:fc AS JSONB), :err)"),
        {"id": run_id, "p": PID,
         "spec": json.dumps({"instructions": "build it", "acceptance_criteria": ["works"],
                             "test_commands": [], "owner": "o", "repo": "r",
                             "base_branch": "main"}),
         "fc": json.dumps(fctx), "err": error},
    )
    return run_id


# --- substrate resolution -----------------------------------------------------
def test_resolve_use_microvm_override_wins():
    s = get_settings()
    orig = s.governed_sandbox_provider
    try:
        s.governed_sandbox_provider = "microvm"
        assert _resolve_use_microvm(s, {}) is True                      # project setting
        assert _resolve_use_microvm(s, {"substrate": "local"}) is False  # per-run override wins
        s.governed_sandbox_provider = "local"
        assert _resolve_use_microvm(s, {}) is False
        assert _resolve_use_microvm(s, {"substrate": "microvm"}) is True
    finally:
        s.governed_sandbox_provider = orig


def test_start_code_task_persists_substrate(engine):
    with begin(engine) as c:
        run_id = start_code_task(
            c, project_id=PID, task_id=None, title="t", instructions="i",
            acceptance_criteria=[], test_commands=[], owner="o", repo="r",
            base_branch="main", finalize_ctx=None, jobs=JOBS, substrate="local")
        spec = c.execute(text("SELECT spec FROM code_task_runs WHERE id=:id"),
                         {"id": run_id}).scalar_one()
        assert spec["substrate"] == "local"


# --- retry fallback + circuit breaker ----------------------------------------
def test_retry_after_microvm_infra_failure_falls_back_to_local(engine):
    with begin(engine) as c:
        prev = _insert_failed_run(c, substrate="microvm")
    out = retry_code_task(engine, get_settings(), JOBS, prev)
    with begin(engine) as c:
        spec = c.execute(text("SELECT spec FROM code_task_runs WHERE id=:id"),
                         {"id": out["run_id"]}).scalar_one()
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
                         {"id": out["run_id"]}).scalar_one()
    assert spec["substrate"] == "local"                                 # degraded-mode fallback
    assert any("substrate_fallback" in h for h in fctx["feedback_history"])


def test_circuit_breaker_refuses_blind_retry_on_local(engine):
    with begin(engine) as c:
        prev = _insert_failed_run(c, substrate="local_subprocess", attempt=2,
                                  error="RuntimeError with no message")
    with pytest.raises(ValueError, match="circuit breaker"):
        retry_code_task(engine, get_settings(), JOBS, prev)
    # an explicit fix_note (human judgment) bypasses the breaker
    out = retry_code_task(engine, get_settings(), JOBS, prev, fix_note="sandbox agent restarted")
    assert out["status"] == "queued"


def test_non_infra_failure_keeps_substrate(engine):
    with begin(engine) as c:
        prev = _insert_failed_run(c, failure_class="terminal", substrate="microvm",
                                  error="tests failed")
    out = retry_code_task(engine, get_settings(), JOBS, prev)
    with begin(engine) as c:
        spec = c.execute(text("SELECT spec FROM code_task_runs WHERE id=:id"),
                         {"id": out["run_id"]}).scalar_one()
    assert "substrate" not in spec                                      # task's fault, not infra's


# --- guest error message never blank ------------------------------------------
def _guest():
    spec = importlib.util.spec_from_file_location(
        "in_guest_agent", "sandbox-host/guest/in_guest_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rc,o,e,needle", [
    (-9, "", "", "killed by signal 9"),                 # OOM-kill signature
    (1, "", "", "exited 1 with no output"),             # silent CLI death
    (1, "", "auth token expired", "auth token expired"),  # real stderr passes through
])
def test_guest_agent_error_never_blank(monkeypatch, rc, o, e, needle):
    mod = _guest()
    monkeypatch.setattr(mod, "_run", lambda *a, **k: (rc, o, e))
    with pytest.raises(RuntimeError) as ei:
        mod._run_agent({"engine": "claude_code", "title": "t", "instructions": "i"})
    msg = str(ei.value)
    assert needle in msg and f"rc={rc}" in msg
    assert msg.strip() != "claude agent failed:"        # the old blind failure is impossible now


# --- pipeline-exception classification ----------------------------------------
def test_classify_pipeline_exception():
    from apps.api.app.services.code_task_service import classify_pipeline_exception
    # max-turns exhaustion is the TASK needing more turns — recoverable, no substrate fallback
    fc, rc = classify_pipeline_exception(
        'claude code agent exited 1: {"subtype":"error_max_turns","num_turns":41}')
    assert (fc, rc) == ("recoverable", "max_turns_exhausted")
    # anything else unexpected stays infra
    assert classify_pipeline_exception("claude agent failed (rc=-9): killed by signal 9") == (
        "infra", "pipeline_exception")


def test_max_turns_retry_does_not_trip_breaker_or_fallback(engine):
    with begin(engine) as c:
        prev = _insert_failed_run(c, failure_class="recoverable", substrate="local_subprocess",
                                  attempt=3, error="error_max_turns at 41")
    out = retry_code_task(engine, get_settings(), JOBS, prev)     # no fix_note needed
    assert out["status"] == "queued"
    with begin(engine) as c:
        spec = c.execute(text("SELECT spec FROM code_task_runs WHERE id=:id"),
                         {"id": out["run_id"]}).scalar_one()
    assert "substrate" not in spec                                # no fallback for task-shaped failures
