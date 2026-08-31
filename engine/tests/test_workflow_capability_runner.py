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
        for table in ("workflow_mappings", "workflow_versions", "workflow_definitions",
                      "decisions", "events", "receipts"):
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


def _start(c, project_id, version_id):
    return workflows.start_run(
        c, project_id=project_id, actor="test", input_data={},
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
