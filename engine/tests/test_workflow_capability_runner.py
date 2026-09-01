"""A customer's delivery workflow actually runs.

Phase 1's claim is that a capability step gets executed, its output flows into the next step,
a gate stops for a person, and a failure records a reason someone can act on. This exercises
that against a real database with a recording tool registry, so the dispatch is real and the
side effects are not — a test suite that opens genuine pull requests is a test suite nobody
can run twice.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)


class RecordingTool:
    """Stands in for a real tool. Records the call and returns what the real one produces."""

    def __init__(self, name, returns):
        self.name = name
        self.kind = "write"
        self.description = name
        self.parameters = {}
        self._returns = returns
        self.calls: list[dict] = []

    def handler(self, ctx, *, project_id, actor, args):
        self.calls.append({"project_id": project_id, "actor": actor, "args": dict(args)})
        return dict(self._returns)


class RecordingRegistry:
    def __init__(self, tools):
        self._tools = tools

    def specs(self):
        return list(self._tools)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_step_runs LIMIT 1"))
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    with engine.begin() as c:
        for ddl in (
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'system'",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS is_catalogue BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS forked_from_version_id TEXT",
        ):
            c.exec_driver_sql(ddl)
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'capability runner test', :s)"),
            {"i": pid, "w": wid, "s": pid},
        )
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM workflow_step_runs WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM workflow_runs WHERE project_id=:p"), {"p": pid})
        # deployment_targets has no ON DELETE CASCADE, so a leftover row blocks the project
        # delete and every later run of this module fails on setup instead of on its subject.
        for table in ("workflow_mappings", "workflow_versions", "workflow_definitions",
                      "decisions", "events", "receipts",
                      # A settled failure deadletters on the FIRST attempt, so these rows now
                      # exist where before three retries were still pending at teardown.
                      "deadletters", "quality_gates",
                      "deployment_health_samples", "deployment_targets"):
            try:
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class Ctx:
    """Minimal engine context: the tools under test only need a transaction."""

    def __init__(self, engine):
        self._engine = engine

    def begin(self):
        return self._engine.begin()


def _publish(c, project_id, key, steps, entry):
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product",
    )
    version = workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test",
    )
    return definition, version


def _start(c, project_id, version_id, input_data=None):
    return workflows.start_run(
        c, project_id=project_id, actor="test", input_data=input_data or {},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id,
    )


def _steps(c, run_id):
    rows = c.execute(
        text("SELECT step_id, status, output, last_error FROM workflow_step_runs "
             "WHERE workflow_run_id=:r ORDER BY created_at"),
        {"r": run_id},
    ).mappings().all()
    return {r["step_id"]: dict(r) for r in rows}



def _finish_code_task(c, project_id, run_id, status):
    """Land a code_task_runs row in a terminal state, as the pipeline would."""
    c.execute(
        text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
             "VALUES (:id,:p,:s,'done','awaited') "
             "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status"),
        {"id": run_id, "p": project_id, "s": status},
    )


# ── The end-to-end claim ───────────────────────────────────────────────────────

def test_a_capability_step_is_executed_by_dispatching_to_a_real_tool(eng, project, monkeypatch):
    """The step calls the tool BOSS already has, rather than a second implementation of it."""
    meeting = RecordingTool("request_meeting", {"meeting_run_id": "mrun_1", "task_id": "tsk_1"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "one_step", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "Build a thing"}},
             "next": []},
        ], "m")
        run = _start(c, project, version["id"])
        result = runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert meeting.calls, "the tool was never called"
    assert meeting.calls[0]["args"] == {"directive": "Build a thing"}
    # STARTED, NOT FINISHED. `request_meeting` returns a run id in milliseconds for a panel that
    # takes twenty minutes. Completing here let a workflow race past its own work and finish in
    # seconds, in a shape that looked like success.
    assert steps["m"]["status"] == "waiting"
    assert steps["m"]["output"]["meeting_run_id"] == "mrun_1"
    assert steps["m"]["output"]["await"] == {"kind": "meeting", "id": "mrun_1"}
    assert result["performed"][0]["outcome"] == "awaiting"


def test_one_steps_output_becomes_the_next_steps_input(eng, project, monkeypatch):
    """"Deploy the thing you just built" is the most ordinary sentence in delivery, and without
    this a workflow could only ever pass literals."""
    build = RecordingTool("create_code_task", {"code_task_run_id": "crun_42"})
    preview = RecordingTool("start_preview", {"preview_id": "prev_9"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build, preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "two_step", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build",
                         "inputs": {"title": "X", "instructions": "do it"}},
             "next": ["p"]},
            {"id": "p", "kind": "capability",
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"from_step": "b", "field": "code_task_run_id"}}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)     # build starts, and parks
        # The build finishes. Only then may the next step run — which is the whole point of
        # waiting, and what makes "deploy the thing you just built" mean anything.
        _finish_code_task(c, project, "crun_42", "completed")
        runner.tick(c, Ctx(eng), project_id=project)     # build resumes, preview claims
        workflows.advance_run(c, project, run["id"])
        runner.tick(c, Ctx(eng), project_id=project)     # preview
        steps = _steps(c, run["id"])

    assert preview.calls, "the second step never ran"
    assert preview.calls[0]["args"] == {"run_id": "crun_42"}, "the reference did not resolve"
    assert steps["b"]["status"] == "succeeded"
    assert steps["p"]["status"] == "succeeded"


def test_a_reference_to_something_never_produced_fails_with_the_reason(eng, project, monkeypatch):
    """Not "step failed". The operator has to be able to see WHICH field was missing."""
    build = RecordingTool("create_code_task", {"something_else": "x"})
    preview = RecordingTool("start_preview", {"preview_id": "p"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build, preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "bad_ref", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "d"}},
             "next": ["p"]},
            {"id": "p", "kind": "capability",
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"from_step": "b", "field": "code_task_run_id"}}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        workflows.advance_run(c, project, run["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not preview.calls, "the tool ran with an unresolved input"
    assert "code_task_run_id" in (steps["p"]["last_error"] or "")


def test_a_gate_waits_for_a_person_instead_of_being_executed(eng, project, monkeypatch):
    """A gate is not work. It raises a decision and stays waiting — the runtime's own
    mechanism — and is never dispatched to a tool."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "gated", [
            {"id": "g", "kind": "capability",
             "binding": {"capability": "spec_review", "inputs": {}},
             "next": []},
        ], "g")
        run = _start(c, project, version["id"])
        result = runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        decisions = c.execute(
            text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": project}
        ).scalar()

    assert steps["g"]["status"] == "waiting"
    assert decisions == 1, "no decision was raised for the gate"
    assert result["performed"][0]["outcome"] == "gate_opened"


def test_a_tool_that_fails_records_a_reason_and_stays_retryable(eng, project, monkeypatch):
    failing = RecordingTool("request_meeting", {"ok": False, "error": "the panel is full"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([failing]))

    with eng.begin() as c:
        _, version = _publish(c, project, "failing", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "d"}}, "next": []},
        ], "m")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert "the panel is full" in (steps["m"]["last_error"] or "")
    assert steps["m"]["status"] in {"ready", "failed"}      # retryable, not deadlettered


def test_a_missing_tool_says_so_rather_than_failing_obscurely(eng, project, monkeypatch):
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        _, version = _publish(c, project, "no_tool", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "d"}}, "next": []},
        ], "m")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
    assert "does not have the 'request_meeting' tool" in (steps["m"]["last_error"] or "")


# ── The line the runner must not cross ─────────────────────────────────────────

def test_the_runner_never_touches_a_system_workflow(eng, project, monkeypatch):
    """BOSS's own definitions are claimed by the machinery that has always claimed them.

    A runner that reached across the namespace could execute the governed coding pipeline a
    second time, concurrently with the real one.
    """
    tool = RecordingTool("request_meeting", {"meeting_run_id": "m"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))

    with eng.begin() as c:
        definition = workflows.create_definition(
            c, project_id=project, workflow_key="internal_thing", name="internal",
            description=None, actor="test", namespace="system",
        )
        version = workflows.publish_version(
            c, project_id=project, definition_id=definition["id"],
            spec={"entry_step": "m", "steps": [
                {"id": "m", "kind": "capability",
                 "binding": {"capability": "meeting", "inputs": {"directive": "d"}},
                 "next": []},
            ]}, actor="test",
        )
        _start(c, project, version["id"])
        result = runner.tick(c, Ctx(eng), project_id=project)

    assert result["count"] == 0, "the runner reached into a system workflow"
    assert not tool.calls


# ── Approval policy (Phase 5) ──────────────────────────────────────────────────

def _gate_workflow(c, project, key, approval):
    return _publish(c, project, key, [
        {"id": "g", "kind": "capability",
         "binding": {"capability": "spec_review", "inputs": {}},
         "approval": approval, "next": []},
    ], "g")


def _overdue(c, run_id):
    """Move a waiting gate's deadline into the past."""
    c.execute(
        text(
            "UPDATE workflow_step_runs "
            "SET binding_snapshot = binding_snapshot || "
            "    jsonb_build_object('due_at', (now() - interval '1 hour')::text) "
            "WHERE workflow_run_id=:r AND status='waiting'"
        ),
        {"r": run_id},
    )


def test_a_gate_records_its_deadline_where_the_waiting_row_can_be_read(eng, project, monkeypatch):
    """A timeout you cannot see from the row that is waiting is a timeout nobody can explain
    when it fires."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        _, version = _gate_workflow(c, project, "sla_gate",
                                    {"sla_hours": 24, "on_timeout": "escalate",
                                     "approver_role": "product_owner"})
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        snapshot = c.execute(
            text("SELECT binding_snapshot FROM workflow_step_runs WHERE workflow_run_id=:r"),
            {"r": run["id"]},
        ).scalar()
    assert snapshot["due_at"], "the gate has no deadline"
    assert snapshot["approval"]["approver_role"] == "product_owner"


def test_an_overdue_gate_that_blocks_stays_waiting(eng, project, monkeypatch):
    """The default. A gate whose unanswered outcome is "proceed" is not a gate."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        _, version = _gate_workflow(c, project, "block_gate", {"sla_hours": 1})
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        _overdue(c, run["id"])
        result = runner.sweep_gate_timeouts(c, project_id=project)
        status = c.execute(
            text("SELECT status FROM workflow_step_runs WHERE workflow_run_id=:r"),
            {"r": run["id"]},
        ).scalar()
    assert result["applied"][0]["outcome"] == "blocked"
    assert status == "waiting", "a blocked gate let the work through"


def test_an_overdue_gate_that_escalates_says_so_once(eng, project, monkeypatch):
    """Escalating every minute is how people learn to ignore escalations."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        _, version = _gate_workflow(c, project, "esc_gate",
                                    {"sla_hours": 1, "on_timeout": "escalate"})
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        _overdue(c, run["id"])
        first = runner.sweep_gate_timeouts(c, project_id=project)
        second = runner.sweep_gate_timeouts(c, project_id=project)
        events = c.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p "
                 "AND event_type='workflow.gate_escalated'"),
            {"p": project},
        ).scalar()
    assert first["applied"][0]["outcome"] == "escalated"
    assert second["count"] == 0, "the gate escalated twice"
    assert events == 1


def test_an_auto_approval_records_that_policy_made_it(eng, project, monkeypatch):
    """"Approved" alone is what an audit fails on. The record has to say approved BY WHAT, and
    under which SLA."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    with eng.begin() as c:
        # `notify` is below the ceiling, so it may approve on timeout.
        _, version = _publish(c, project, "auto_gate", [
            {"id": "n", "kind": "capability",
             "binding": {"capability": "notify", "inputs": {"message": "hi"}},
             "approval": {"mode": "manual", "sla_hours": 1, "on_timeout": "approve"},
             "next": []},
        ], "n")
        run = _start(c, project, version["id"])
        # Manual-mode non-gate steps are executed normally; force the waiting state the sweep
        # acts on, which is what a real approval flow would have produced.
        c.execute(
            text("UPDATE workflow_step_runs SET status='waiting', "
                 "binding_snapshot = jsonb_build_object("
                 "  'approval', CAST(:a AS JSONB), "
                 "  'due_at', (now() - interval '1 hour')::text) "
                 "WHERE workflow_run_id=:r"),
            {"r": run["id"], "a": '{"on_timeout":"approve","sla_hours":1}'},
        )
        result = runner.sweep_gate_timeouts(c, project_id=project)
        row = c.execute(
            text("SELECT status, output, evidence FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r"),
            {"r": run["id"]},
        ).mappings().one()
    assert result["applied"][0]["outcome"] == "auto_approved"
    assert row["status"] == "succeeded"
    assert row["output"]["approved_by"] == "policy"
    assert row["evidence"][0]["kind"] == "auto_approval"
    assert row["evidence"][0]["sla_hours"] == 1


# ── Started is not finished ────────────────────────────────────────────────────

def test_a_build_that_is_still_running_does_not_release_the_next_step(eng, project, monkeypatch):
    """THE DEFECT THIS FIXES. `create_code_task` returns a run id in milliseconds for a build
    that takes twenty minutes. Completing the step on that return let a workflow race past its
    own work: build, review and test all 'done' while the build was still cloning, and the run
    finished in seconds in a shape that looked like success."""
    build = RecordingTool("create_code_task", {"run_id": "crun_slow"})
    preview = RecordingTool("start_preview", {"preview_id": "prev_1"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build, preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "wait_one", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": ["p"]},
            {"id": "p", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_slow"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_slow", "running")   # still going
        runner.tick(c, Ctx(eng), project_id=project)
        workflows.advance_run(c, project, run["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["b"]["status"] == "waiting", "the build step did not wait"
    assert not preview.calls, "the next step ran while the build was still going"


def test_a_build_that_failed_fails_its_step_rather_than_shipping(eng, project, monkeypatch):
    """Silently succeeding would let a workflow report a shipped product on top of a build that
    did not build. The underlying status travels into the message so the reason is not lost."""
    build = RecordingTool("create_code_task", {"run_id": "crun_bad"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "wait_fail", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_bad", "failed")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        status = c.execute(
            text("SELECT status FROM workflow_runs WHERE id=:i"), {"i": run["id"]}).scalar()

    assert steps["b"]["status"] == "failed"
    assert "failed" in (steps["b"]["last_error"] or "")
    assert status == "failed", "the run reported success over a failed build"


def test_a_capability_with_no_correlation_id_is_not_parked(eng, project, monkeypatch):
    """A tool that claims to be async but hands back nothing to wait ON cannot be waited on.
    Parking it would hang the run forever with no way to say why, so it completes as before."""
    build = RecordingTool("create_code_task", {"ok": True})     # no id of any kind
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "no_id", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["b"]["status"] == "succeeded"


def test_a_synchronous_capability_still_completes_immediately(eng, project, monkeypatch):
    """Only the async ones wait. Making everything wait would stall workflows on steps that
    genuinely finish when their tool returns."""
    preview = RecordingTool("start_preview", {"preview_id": "prev_2"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "sync_one", [
            {"id": "p", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_x"}},
             "next": []},
        ], "p")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["p"]["status"] == "succeeded"


def test_the_parked_step_holds_no_lease(eng, project, monkeypatch):
    """A lease held for the twenty minutes of a build would be reclaimed as a crashed worker,
    and the step re-run — starting a SECOND build."""
    build = RecordingTool("create_code_task", {"run_id": "crun_lease"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "lease_one", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, lease_owner, lease_expires_at FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r"), {"r": run["id"]}).mappings().first()

    assert row["status"] == "waiting"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None


# ── The outer run adopts the work the console already started ──────────────────

def test_build_adopts_the_code_task_the_project_was_started_with(eng, project, monkeypatch):
    """The console creates the code task and THEN starts the outer run, because the operator
    pressed a button that makes a project. If `build` also called create_code_task there would
    be two builds racing over one repository."""
    build = RecordingTool("create_code_task", {"run_id": "crun_second"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "adopt", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build",
                         "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_first"})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not build.calls, "a SECOND build was started over the same repository"
    assert steps["b"]["status"] == "waiting"
    assert steps["b"]["output"]["await"]["id"] == "crun_first"


def test_the_adopted_build_completes_when_that_task_does(eng, project, monkeypatch):
    build = RecordingTool("create_code_task", {"run_id": "crun_unused"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "adopt2", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_adopted"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_adopted", "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["b"]["status"] == "succeeded"


def _decision(c, project_id, decision_id, status):
    """A decision row in a given state, as the PRD approval leaves behind."""
    c.execute(
        text("INSERT INTO decisions (id, project_id, question, status, approved_by, approved_at) "
             "VALUES (:i,:p,'approve the PRD',:s,'ceo',now()) "
             "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status"),
        {"i": decision_id, "p": project_id, "s": status},
    )


def test_meeting_adopts_the_panel_the_project_came_from(eng, project, monkeypatch):
    """A project born from a PRD panel already HAD its meeting — that panel is why the project
    exists. Convening another one debates a question that was settled, at the cost of a full
    agent panel, before the build it is meant to precede."""
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_second"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "adoptmeet", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "go"}},
             "next": []},
        ], "m")
        run = _start(c, project, version["id"], {"adopt_meeting_run_id": "mrun_first"})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not meeting.calls, "a SECOND panel was convened for a settled question"
    assert steps["m"]["status"] == "waiting"
    assert steps["m"]["output"]["await"]["id"] == "mrun_first"


def test_the_build_step_names_its_task_the_way_deploy_reads_it(eng, project, monkeypatch):
    """The catalogue's `preview` and `deploy` bind {"from_step": "build", "field":
    "code_task_run_id"}. create_code_task returns `run_id`, and an adopted build calls no tool at
    all — so that field existed in the catalogue and nowhere else, and every standard workflow
    would have failed at preview with "step 'build' did not produce 'code_task_run_id'"."""
    build = RecordingTool("create_code_task", {"run_id": "crun_started"})
    preview = RecordingTool("start_preview", {"preview_url": "https://example.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build, preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "canon", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": ["preview"]},
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"from_step": "build",
                                               "field": "code_task_run_id"}}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_started", "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["build"]["output"]["code_task_run_id"] == "crun_started"
    assert steps["preview"]["status"] == "succeeded", steps["preview"]["last_error"]
    assert preview.calls[0]["args"]["run_id"] == "crun_started"


def test_an_adopted_build_is_referenced_the_same_way(eng, project, monkeypatch):
    """An adopted build calls no tool, so its output has no id at all unless one is written."""
    preview = RecordingTool("start_preview", {"preview_url": "https://example.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "canon2", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": ["preview"]},
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"from_step": "build",
                                               "field": "code_task_run_id"}}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_adopt_ref"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_adopt_ref", "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["preview"]["status"] == "succeeded", steps["preview"]["last_error"]
    assert preview.calls[0]["args"]["run_id"] == "crun_adopt_ref"


def _quality_gate(c, project_id, run_id, *, passed, failed, tests_passed=True, evidence=None):
    import json as _json
    c.execute(
        text("INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, "
             "tests_passed, adversarial_verdict, status, gates_passed, gates_failed, evidence) "
             "VALUES (:i,:p,:r,2,:tp,'survived',:st,CAST(:gp AS JSONB),CAST(:gf AS JSONB),"
             "CAST(:ev AS JSONB)) ON CONFLICT (project_id, code_task_run_id) DO UPDATE SET "
             "gates_passed=EXCLUDED.gates_passed, gates_failed=EXCLUDED.gates_failed"),
        {"i": f"qg_{run_id}", "p": project_id, "r": run_id, "tp": tests_passed,
         "st": "pass" if not failed else "fail",
         "gp": _json.dumps(passed), "gf": _json.dumps(failed),
         "ev": _json.dumps(evidence or {"steps_run": ["implement", "review_code"]})},
    )


def _observing_workflow(c, project, key, step_key):
    return _publish(c, project, key, [
        {"id": "build", "kind": "capability",
         "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
         "next": [step_key]},
        {"id": step_key, "kind": "capability", "binding": {"capability": step_key}, "next": []},
    ], "build")


def test_review_reports_what_the_pipeline_actually_found(eng, project, monkeypatch):
    """`review` commissions nothing — the governed pipeline already reviewed the build and wrote
    the verdicts down. Reporting "recorded from the work it accompanies" left three of the nine
    steps saying nothing about work that had definitely happened."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obsrev", "review")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_reviewed"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_reviewed", "completed")
        _quality_gate(c, project, "crun_reviewed",
                      passed=["code_review_passed", "security_clean", "consistent"], failed=[])
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    out = steps["review"]["output"]
    assert steps["review"]["status"] == "succeeded", steps["review"]["last_error"]
    assert out["status"] == "observed"
    assert set(out["gates_passed"]) == {"code_review_passed", "security_clean", "consistent"}
    assert out["code_task_run_id"] == "crun_reviewed"


def test_a_blocking_finding_asks_you_instead_of_killing_the_run(eng, project, monkeypatch):
    """A finding meant to stop the line is a DECISION, not a dead end.

    Deadlettering answered it for the operator — "no, do not continue" — with no way to say
    "ship it anyway", and left preview, deploy and monitor pending forever on work that was
    already built. Measured on wfr_c8578ea1abbf4a1383cc: the run deadlettered at `review` while
    the app it had built was previewable in a browser."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obsfail", "review")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_badreview"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_badreview", "completed")
        _quality_gate(c, project, "crun_badreview",
                      passed=["consistent"], failed=["security_clean"])
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, decision_id, output FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='review'"),
            {"r": run["id"]},
        ).mappings().first()
        question = c.execute(text("SELECT question FROM decisions WHERE id=:d"),
                             {"d": row["decision_id"]}).scalar()

    assert row["status"] == "waiting", "the run was killed instead of asking"
    assert row["decision_id"], "no decision was raised for a blocking finding"
    assert "security_clean" in question and "anyway" in question
    assert row["output"]["findings"] == ["security_clean"]


def test_an_advisory_finding_does_not_hold_the_line(eng, project, monkeypatch):
    """An advisory first pass surfaces a failing check and ships the pull request anyway, ON
    PURPOSE, so there is something to iterate on. Reading `gates_failed` without reading that
    policy stopped preview and deploy for a build the operator could already open in a
    browser."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obsadvisory", "review")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_advisory"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_advisory", "completed")
        c.execute(
            text("UPDATE code_task_runs SET finalize_ctx = "
                 "jsonb_build_object('advisory_failing_gates', "
                 "CAST('[\"security_clean\"]' AS JSONB)) WHERE id = :r"),
            {"r": "crun_advisory"},
        )
        _quality_gate(c, project, "crun_advisory", passed=[], failed=["security_clean"])
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    out = steps["review"]["output"]
    assert steps["review"]["status"] == "succeeded", steps["review"]["last_error"]
    assert out["advisory"] is True
    assert out["findings"] == ["security_clean"]
    # Not green either: "shipped with findings" and "clean" are different facts.
    assert "shipped with findings" in out["note"]


def test_test_reports_whether_the_tests_passed(eng, project, monkeypatch):
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obstest", "test")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_tested"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_tested", "completed")
        _quality_gate(c, project, "crun_tested", passed=["tests_strong_enough", "nothing_broke"],
                      failed=[], tests_passed=True)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["test"]["status"] == "succeeded", steps["test"]["last_error"]
    assert steps["test"]["output"]["tests_passed"] is True


def test_a_failing_suite_asks_rather_than_reading_green(eng, project, monkeypatch):
    """`tests_passed` is not one of the named gates, so it is checked on its own — a suite that
    did not pass, on the step whose whole subject is whether it did, must not read green."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obstestfail", "test")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_redtests"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_redtests", "completed")
        _quality_gate(c, project, "crun_redtests", passed=["nothing_broke"], failed=[],
                      tests_passed=False)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, decision_id, output FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='test'"),
            {"r": run["id"]},
        ).mappings().first()

    assert row["status"] == "waiting"
    assert row["decision_id"]
    assert "tests_passed" in row["output"]["findings"]


def test_no_recorded_gate_says_so_rather_than_inventing_a_verdict(eng, project, monkeypatch):
    """A build with no quality gate is a build nobody graded. Claiming it reviewed clean would be
    worse than admitting there is nothing to report."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _observing_workflow(c, project, "obsnone", "review")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_ungraded"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_ungraded", "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["review"]["status"] == "succeeded"
    assert steps["review"]["output"]["status"] == "recorded"


def _deployment_target(c, project_id, target_id, *, status="active"):
    import json as _json
    c.execute(
        text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
             "target_kind, target_ref, domains, health_checks, status, created_by) "
             "VALUES (:i,:p,'app','o/r','production','docker','ref',"
             "CAST(:d AS JSONB),CAST(:h AS JSONB),:s,'test') "
             "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status"),
        {"i": target_id, "p": project_id, "d": _json.dumps(["https://app.test"]),
         "h": _json.dumps([{"path": "/healthz"}]), "s": status},
    )


def _monitor_workflow(c, project, key):
    return _publish(c, project, key, [
        {"id": "monitor", "kind": "capability", "binding": {"capability": "monitor"},
         "next": []},
    ], "monitor")


def test_preview_adopts_one_that_is_already_serving(eng, project, monkeypatch):
    """The build pipeline starts a preview itself. Calling start_preview again boots a SECOND
    copy of the same app on a second port, and tells the operator — who has the first one open
    in a browser — about a different URL."""
    preview = RecordingTool("start_preview", {"preview_url": "https://second.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        c.execute(
            text("INSERT INTO app_previews (id, project_id, run_id, url, status) "
                 "VALUES (:i,:p,:r,'https://already.test','running') "
                 "ON CONFLICT (id) DO NOTHING"),
            {"i": f"prev_{project}", "p": project, "r": "crun_previewed"},
        )
        _, version = _publish(c, project, "prevadopt", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_previewed"}},
             "next": []},
        ], "preview")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM app_previews WHERE project_id=:p"), {"p": project})

    assert not preview.calls, "a second copy of the app was booted on a second port"
    assert steps["preview"]["output"]["preview_url"] == "https://already.test"


def test_preview_still_starts_one_when_there_is_none(eng, project, monkeypatch):
    """Adopt-or-start: the workflow must DRIVE when there is nothing to adopt."""
    preview = RecordingTool("start_preview", {"preview_url": "https://started.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "prevstart", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_nopreview"}},
             "next": []},
        ], "preview")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert preview.calls, "the workflow observed instead of driving"
    assert steps["preview"]["output"]["preview_url"] == "https://started.test"


def test_a_settled_verdict_fails_once_instead_of_three_times(eng, project, monkeypatch):
    """A tool call can fail because a network blipped, and running it again is right. A recorded
    fact is not like that — re-reading the row reaches the same conclusion. Monitoring is the
    remaining settled case: no active target means no probe will ever run, however many times
    the step is retried."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _monitor_workflow(c, project, "settledmon")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, attempt FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='monitor'"),
            {"r": run["id"]},
        ).mappings().first()

    assert row["status"] == "deadlettered", "a settled verdict was queued for another attempt"
    assert row["attempt"] == 1, f"tried {row['attempt']} times to re-read one recorded fact"


def test_an_ordinary_failure_is_still_retried(eng, project, monkeypatch):
    """Only SETTLED failures skip the retry. A tool that failed transiently must still get its
    attempts — making everything permanent would turn one bad minute into a dead run."""
    flaky = RecordingTool("create_code_task", {"ok": False, "error": "connection reset"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([flaky]))

    with eng.begin() as c:
        _, version = _publish(c, project, "transient", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, attempt FROM workflow_step_runs WHERE workflow_run_id=:r"),
            {"r": run["id"]},
        ).mappings().first()

    assert row["status"] == "ready", "a transient failure was made permanent"


def test_monitoring_names_what_is_actually_being_watched(eng, project, monkeypatch):
    """"Set up monitoring" recorded a sentence and checked nothing. BOSS already probes every
    active deployment target on a sweep, so the honest step names the targets and says what the
    probes have found."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _deployment_target(c, project, f"dt_{project}")
        _, version = _monitor_workflow(c, project, "monwatch")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM deployment_targets WHERE project_id=:p"), {"p": project})

    out = steps["monitor"]["output"]
    assert steps["monitor"]["status"] == "succeeded", steps["monitor"]["last_error"]
    assert out["status"] == "observed"
    assert out["targets"][0]["url"] == "https://app.test"
    # Registered but not yet swept — "we are watching" is not "we looked and it was fine".
    assert out["targets"][0]["last_probe"] is None
    assert "none probed yet" in out["note"]


def test_monitoring_fails_when_nothing_is_watching(eng, project, monkeypatch):
    """A step called "Set up monitoring" that succeeds while nothing probes the app is worse
    than a green review over a red gate: the entire point is that somebody notices when it
    falls over."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _monitor_workflow(c, project, "monnone")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["monitor"]["status"] != "succeeded"
    assert "nothing is being watched" in (steps["monitor"]["last_error"] or "")


def test_a_target_that_was_retired_is_not_counted_as_watched(eng, project, monkeypatch):
    """Only `active` targets are swept, so an inactive one is a target nothing probes."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _deployment_target(c, project, f"dt_off_{project}", status="retired")
        _, version = _monitor_workflow(c, project, "monretired")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM deployment_targets WHERE project_id=:p"), {"p": project})

    assert steps["monitor"]["status"] != "succeeded"


def test_research_adopts_the_discovery_the_work_came_from(eng, project, monkeypatch):
    discovery = RecordingTool("create_discovery", {"run_id": "drun_second"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    with eng.begin() as c:
        _, version = _publish(c, project, "adoptdisc", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        run = _start(c, project, version["id"], {"adopt_discovery_run_id": "drun_first"})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not discovery.calls, "research was commissioned twice for one project"
    assert steps["r"]["output"]["await"]["id"] == "drun_first"


def test_a_retrospective_run_does_not_research_what_is_already_built(eng, project, monkeypatch):
    """A run started with an adopted build is retrospective: the code was already being written
    when the workflow began. Running `research` forwards there commissions web research, at real
    cost, for a thing that exists — and `meeting` convenes a panel to debate a settled question.
    Recorded as not applicable, which is what actually happened."""
    discovery = RecordingTool("create_discovery", {"run_id": "drun_x"})
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_x"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "retro", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": ["m"]},
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "go"}}, "next": []},
        ], "r")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_live"})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not discovery.calls and not meeting.calls, "money was spent re-deciding a built thing"
    assert steps["r"]["status"] == "succeeded"
    assert steps["r"]["output"]["status"] == "not_applicable"
    assert "started directly" in steps["r"]["output"]["note"]
    assert steps["m"]["output"]["status"] == "not_applicable"


def test_a_run_with_nothing_adopted_still_researches(eng, project, monkeypatch):
    """The ordinary forward path — a workflow started on its own, with no build in flight — must
    be untouched by any of this."""
    discovery = RecordingTool("create_discovery", {"run_id": "drun_fwd"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    with eng.begin() as c:
        _, version = _publish(c, project, "forward", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)

    assert discovery.calls, "the ordinary forward path stopped doing research"


def test_an_approved_spec_is_not_approved_again(eng, project, monkeypatch):
    """The panel path approves the spec BEFORE the project exists. Opening a spec_review gate
    then asks the same person the same question a second time, about a build already running —
    and until they answer it, the workflow sits waiting on work that has already happened."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _decision(c, project, "dec_approved", "approved")
        _, version = _publish(c, project, "adoptgate", [
            {"id": "g", "kind": "capability", "binding": {"capability": "spec_review"},
             "next": []},
        ], "g")
        run = _start(c, project, version["id"], {"adopt_decision_id": "dec_approved"})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["g"]["status"] == "succeeded"
    assert steps["g"]["output"]["decision_id"] == "dec_approved"
    # Who approved it is still on the record — adopting an approval must not lose the audit
    # trail that a fresh gate would have written.
    assert steps["g"]["output"]["approved_by"] == "ceo"


def test_a_spec_that_was_NOT_approved_is_still_asked(eng, project, monkeypatch):
    """An id is not evidence. A run carrying the id of a decision that was rejected, or one
    still pending, must have its gate opened — otherwise passing any decision id at all would
    wave a build past its own review."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _decision(c, project, "dec_rejected", "rejected")
        _, version = _publish(c, project, "adoptgate2", [
            {"id": "g", "kind": "capability", "binding": {"capability": "spec_review"},
             "next": []},
        ], "g")
        run = _start(c, project, version["id"], {"adopt_decision_id": "dec_rejected"})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        opened = c.execute(
            text("SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id=:r"),
            {"r": run["id"]},
        ).scalar()

    assert steps["g"]["status"] == "waiting"
    assert opened and opened != "dec_rejected", "no fresh approval was asked for"


def test_a_gate_with_no_adopted_decision_behaves_exactly_as_before(eng, project, monkeypatch):
    """The ordinary path — a project started from the form, with no panel behind it."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "plaingate", [
            {"id": "g", "kind": "capability", "binding": {"capability": "spec_review"},
             "next": []},
        ], "g")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["g"]["status"] == "waiting"


def test_the_runs_own_input_fills_optional_step_inputs(eng, project, monkeypatch):
    """A catalogue `build` step binds only title and instructions. A project started from the
    form also carries acceptance criteria and test commands — real instructions that had
    nowhere to go, so the build ran without the tests it was meant to pass."""
    build = RecordingTool("create_code_task", {"run_id": "crun_opt"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "optfill", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "b")
        _start(c, project, version["id"],
               {"test_commands": ["pytest -q"], "acceptance_criteria": ["it works"]})
        runner.tick(c, Ctx(eng), project_id=project)

    assert build.calls, "the build never ran"
    args = build.calls[0]["args"]
    assert args["test_commands"] == ["pytest -q"]
    assert args["acceptance_criteria"] == ["it works"]


def test_an_explicit_binding_is_never_overridden_by_the_run_input(eng, project, monkeypatch):
    """An explicit binding is a decision by whoever wrote the workflow. Whatever happens to sit
    in the run input under a matching name must not quietly replace it."""
    build = RecordingTool("create_code_task", {"run_id": "crun_bind"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build]))

    with eng.begin() as c:
        _, version = _publish(c, project, "bindwins", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build",
                         "inputs": {"title": "X", "instructions": "go",
                                    "test_commands": ["make check"]}},
             "next": []},
        ], "b")
        _start(c, project, version["id"], {"test_commands": ["pytest -q"]})
        runner.tick(c, Ctx(eng), project_id=project)

    assert build.calls[0]["args"]["test_commands"] == ["make check"]
