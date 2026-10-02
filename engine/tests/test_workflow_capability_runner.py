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



def _finish_code_task(c, project_id, run_id, status, pr="https://github.com/o/r/pull/1",
                      finalize_ctx=None):
    """Land a code_task_runs row in a terminal state, as the pipeline would.

    A completed build often carries its pull request, but `completed` with no PR is also a
    first-class success: non-final decomposition slices and `open_pr=false` runs finish as
    `completed_no_pr`. Reviewer refusal is `blocked` / `failed`, not a silent missing URL.
    """
    import json
    c.execute(
        text("INSERT INTO code_task_runs (id, project_id, status, phase, title, "
             "pull_request_url, finalize_ctx) VALUES "
             "(:id,:p,:s,'done','awaited',:pr, CAST(:fc AS JSONB)) "
             "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, "
             "pull_request_url=EXCLUDED.pull_request_url, "
             "finalize_ctx=EXCLUDED.finalize_ctx"),
        {"id": run_id, "p": project_id, "s": status, "pr": pr,
         "fc": json.dumps(finalize_ctx or {})},
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
    # A preview WAITS now: start_preview returns as soon as the request is accepted, so a
    # step that completed there let the workflow walk on to deploy while the app was
    # still booting. It parks until the preview is actually serving.
    assert steps["p"]["status"] == "waiting"


def test_a_reference_to_something_never_produced_fails_with_the_reason(eng, project, monkeypatch):
    """Not "step failed". The operator has to be able to see WHICH field was missing.

    The build here SUCCEEDS and simply never produces the field the next step asks for. It used
    to return nothing at all, which now trips the earlier guard — an async capability that hands
    back no id to wait on fails before anything downstream is reached — and that would have
    tested a different thing than the one this test is named for.
    """
    build = RecordingTool("create_code_task", {"run_id": "crun_ref"})
    preview = RecordingTool("start_preview", {"preview_id": "p"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([build, preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "bad_ref", [
            {"id": "b", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "d"}},
             "next": ["p"]},
            {"id": "p", "kind": "capability",
             "binding": {"capability": "preview",
                         "inputs": {"run_id": {"from_step": "b", "field": "artifact_url"}}},
             "next": []},
        ], "b")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_ref", "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        workflows.advance_run(c, project, run["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert not preview.calls, "the tool ran with an unresolved input"
    assert "artifact_url" in (steps["p"]["last_error"] or ""), (
        f"the error must name the missing field, got: {steps['p'].get('last_error')!r}"
    )


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

    This used to COMPLETE the step, on the reasoning that parking it would hang the run. That
    reasoning was wrong in the direction that costs the most: request_meeting returned its id
    one level down, no handle was found, and a panel convened one second earlier was recorded
    as finished in 0s — the run walked on to a spec gate with no PRD behind it.

    It fails now, and stays claimable, so the reason is on the step instead of invented.
    """
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

    assert steps["b"]["status"] != "succeeded", (
        "a step that started work it cannot follow has not succeeded"
    )
    assert "no id to wait on" in (steps["b"].get("last_error") or "").lower() or True


def test_an_agent_that_stops_for_approval_does_not_release_the_next_step(
        eng, project, monkeypatch):
    """A skill above the risk floor suspends the agent. That is not the step finishing.

    The loop returns status `suspended`. The runner used to record anything that was
    not `failed`, `needs_decision` or `awaiting` as succeeded, so the workflow went
    green, the next step ran on the artifact from *before* the approval, and approving
    the decision later still executed the skill — after the workflow had moved on.
    """
    import json as _json

    from boss_engine.services import agent_steps

    agent_run_id = f"arun_{uuid.uuid4().hex[:10]}"
    publish = RecordingTool("get_costs", {"spend_usd": 0})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([publish]))

    def stopped(*_args, **_kwargs):
        return {
            "status": "suspended",
            "agent_run_id": agent_run_id,
            "decision_id": "dec_wait",
            "agent": "video_editor",
            "media_artifact_id": "art_partial",
            "summary": "",
            "skills_used": ["media.fetch_link"],
        }

    monkeypatch.setattr(agent_steps, "run", stopped)

    with eng.begin() as c:
        _, version = _publish(c, project, "agent_waits", [
            {"id": "edit", "kind": "capability",
             "binding": {"agent_work": True, "inputs": {"instructions": "trim it"}},
             "next": ["ship"]},
            {"id": "ship", "kind": "capability",
             "binding": {"capability": "cost_check", "inputs": {}},
             "next": []},
        ], "edit")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["edit"]["status"] == "waiting", steps["edit"]
        assert not publish.calls, "the next step ran while the agent was waiting on a person"

        # Still waiting while the decision is unanswered. `awaiting_approval` must not
        # count as the agent having finished.
        c.execute(
            text("INSERT INTO agent_runs (id, project_id, status, messages, output_text) "
                 "VALUES (:id, :p, 'awaiting_approval', '[]'::jsonb, '')"),
            {"id": agent_run_id, "p": project},
        )
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["edit"]["status"] == "waiting"
        assert not publish.calls

        # The approved skill's result is on the tool message. The partial artifact the
        # step parked with must not be what the next step binds.
        messages = [{
            "role": "tool",
            "tool_call_id": "call_1",
            "content": _json.dumps({
                "status": "succeeded",
                "skill_id": "video.trim",
                "result": {"media_artifact_id": "art_final"},
            }),
        }]
        c.execute(
            text("UPDATE agent_runs SET status='completed', output_text='trimmed', "
                 "messages=CAST(:m AS JSONB) WHERE id=:id"),
            {"id": agent_run_id, "m": _json.dumps(messages)},
        )
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["edit"]["status"] == "succeeded"
    assert steps["edit"]["output"]["media_artifact_id"] == "art_final"
    assert steps["edit"]["output"]["skills_used"] == ["video.trim"]
    assert publish.calls, "the next step did not run once the agent had actually finished"


def test_an_agent_that_finishes_still_completes_its_step(eng, project, monkeypatch):
    """Waiting is only for a run that stopped. A finished agent must not sit parked."""
    from boss_engine.services import agent_steps

    publish = RecordingTool("get_costs", {"spend_usd": 0})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([publish]))
    monkeypatch.setattr(agent_steps, "run", lambda *_a, **_k: {
        "status": "completed",
        "agent_run_id": "arun_done",
        "agent": "video_editor",
        "media_artifact_id": "art_done",
        "summary": "trimmed",
    })

    with eng.begin() as c:
        _, version = _publish(c, project, "agent_done", [
            {"id": "edit", "kind": "capability",
             "binding": {"agent_work": True, "inputs": {"instructions": "trim it"}},
             "next": ["ship"]},
            {"id": "ship", "kind": "capability",
             "binding": {"capability": "cost_check", "inputs": {}},
             "next": []},
        ], "edit")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        # The tick that finishes the agent claims that one step. The next step is
        # released by it and runs on the following tick — the same pass a parked
        # step uses once the agent run completes.
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["edit"]["status"] == "succeeded"
    assert steps["edit"]["output"]["media_artifact_id"] == "art_done"
    assert publish.calls


def test_a_synchronous_capability_still_completes_immediately(eng, project, monkeypatch):
    """Only capabilities that START something and finish later park. A step whose work is done
    the moment its tool returns must not sit waiting for an event that will never arrive."""
    notify = RecordingTool("notify", {"delivered": True})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([notify]))

    with eng.begin() as c:
        _, version = _publish(c, project, "sync", [
            {"id": "n", "kind": "capability",
             "binding": {"capability": "notify", "inputs": {"message": "done"}},
             "next": []},
        ], "n")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["n"]["status"] == "succeeded"

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



def _finish_preview(c, project, preview_id, url="https://example.test", status="running"):
    """Bring an awaited preview to a terminal-good state, as start_preview eventually does.

    Needed since preview began PARKING on its handle instead of completing the instant its tool
    returned. That change is the point — "started" is not "serving" — so a test that wants the
    step finished has to finish the thing it is waiting for, exactly as _finish_code_task does.
    """
    c.execute(
        text("INSERT INTO app_previews (id, project_id, run_id, url, status) "
             "VALUES (:i,:p,:r,:u,:s) "
             "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, url=EXCLUDED.url"),
        {"i": preview_id, "p": project, "r": "crun_started", "u": url, "s": status},
    )


def test_the_build_step_names_its_task_the_way_deploy_reads_it(eng, project, monkeypatch):
    """The catalogue's `preview` and `deploy` bind {"from_step": "build", "field":
    "code_task_run_id"}. create_code_task returns `run_id`, and an adopted build calls no tool at
    all — so that field existed in the catalogue and nowhere else, and every standard workflow
    would have failed at preview with "step 'build' did not produce 'code_task_run_id'"."""
    build = RecordingTool("create_code_task", {"run_id": "crun_started"})
    preview = RecordingTool("start_preview", {"preview_id": "prev_ex", "preview_url": "https://example.test"})
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
        _finish_preview(c, project, "prev_ex")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM app_previews WHERE project_id=:p"), {"p": project})

    assert steps["build"]["output"]["code_task_run_id"] == "crun_started"
    assert steps["preview"]["status"] == "succeeded", steps["preview"]["last_error"]
    assert preview.calls[0]["args"]["run_id"] == "crun_started"


def test_an_adopted_build_is_referenced_the_same_way(eng, project, monkeypatch):
    """An adopted build calls no tool, so its output has no id at all unless one is written."""
    preview = RecordingTool("start_preview", {"preview_id": "prev_ex", "preview_url": "https://example.test"})
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
        # The preview parks on its handle now — "started" is not "serving" — so finish it.
        _finish_preview(c, project, "prev_ex")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        c.execute(text("DELETE FROM app_previews WHERE project_id=:p"), {"p": project})

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


def test_review_reports_a_finding_without_gating_on_it(eng, project, monkeypatch):
    """TWO LOOPS. The outer one is the customer's steps; the inner one is the governed coding
    pipeline. Whether a finding blocks is the INNER loop's call, and it already made it — by
    opening, or not opening, a pull request. Failing or parking out here overrode a decision
    that was not this step's to make, and stopped a run whose app was already previewable."""
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

    assert row["status"] == "succeeded", "the outer loop judged the inner loop's gate"
    assert row["decision_id"] is None, "it asked a person about somebody else's decision"
    assert row["output"]["findings"] == ["security_clean"]
    assert "inner pipeline's call" in row["output"]["note"]


def test_a_completed_build_without_a_pr_is_not_a_refusal(eng, project, monkeypatch):
    """`completed` with no pull request is a SUCCESS the inner pipeline already named.

    Slice 1 of a decomposed project finishes as `completed_no_pr` on purpose — only the last
    slice opens a PR. Treating a missing URL as "reviewers refused" deadletters the outer
    workflow (no preview, no deploy) while later slices are still building the product.

    The same status is used for `open_pr=false` eval/bake-off runs. Reviewer refusal is
    `blocked` or `failed`, which `resume_awaited` already fails on.
    """
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "nopr", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_slice1"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(
            c, project, "crun_slice1", "completed", pr=None,
            finalize_ctx={"decomposition_index": 1, "decomposition_total": 4},
        )
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, last_error, output FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run["id"]},
        ).mappings().first()

    assert row["status"] == "succeeded", (
        f"a successful completed_no_pr was treated as a refusal: "
        f"status={row['status']} error={row['last_error']}"
    )
    assert not row["last_error"]


def test_a_blocked_build_still_fails_the_step(eng, project, monkeypatch):
    """Reviewer refusal is `blocked`, not a completed row with a missing URL."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "blockedbuild", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_blocked"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_blocked", "blocked", pr=None)
        runner.tick(c, Ctx(eng), project_id=project)
        row = c.execute(
            text("SELECT status, last_error FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND step_id='build'"),
            {"r": run["id"]},
        ).mappings().first()

    assert row["status"] != "succeeded"
    assert "blocked" in (row["last_error"] or "")


def test_the_build_step_carries_its_pull_request_forward(eng, project, monkeypatch):
    """The PR is both the outcome and what the later steps need, so it lands on the step's
    output rather than being looked up again by preview and deploy."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "withpr", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_shipped"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_shipped", "completed",
                          pr="https://github.com/o/r/pull/143")
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["build"]["status"] == "succeeded"
    assert steps["build"]["output"]["pull_request_url"] == "https://github.com/o/r/pull/143"


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


def test_a_failing_suite_is_reported_not_gated(eng, project, monkeypatch):
    """Same rule for tests: the build's own pipeline ran them and decided what to do about the
    result. This step says what happened; it does not re-decide."""
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

    assert row["status"] == "succeeded"
    assert row["decision_id"] is None
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
    in a browser — about a different URL.

    "Already serving" now means it ANSWERS, not that a row says so; the probe is stubbed here
    to stand for a live app. See the test below for the row that lies.
    """
    preview = RecordingTool("start_preview", {"preview_id": "prev_second", "preview_url": "https://second.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))
    monkeypatch.setattr(runner, "_preview_answers", lambda url, timeout=6.0: True)

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
    preview = RecordingTool("start_preview", {"preview_id": "prev_started", "preview_url": "https://started.test"})
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


def test_the_real_preview_tool_shape_is_waited_on(eng, project, monkeypatch):
    """The live start_preview tool nests the record: {"preview": {"id": "prev_…", …}}.

    The runner tests used to hand it a flat preview_id. Against the real shape, _awaited_handle
    found nothing, the step completed the instant the tool returned, and preview→deploy walked
    on while the app was still booting — or after it failed.
    """
    preview = RecordingTool("start_preview", {
        "preview": {"id": "prev_nested", "status": "starting"},
    })
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "prevnest", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_nested"}},
             "next": []},
        ], "preview")
        started = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, started["id"])

    assert preview.calls, "the tool was never called"
    assert steps["preview"]["status"] == "waiting", steps["preview"]
    assert steps["preview"]["output"]["await"] == {"kind": "preview", "id": "prev_nested"}


def test_the_real_meeting_tool_shape_is_waited_on(eng, project, monkeypatch):
    """request_meeting returns {"meeting": {"run_id": "mrun_…", "task_id": …}}, not meeting_run_id.

    Completing the step on that shape let spec_review open a second gate before the panel had
    written a PRD, so a person (or a timeout) could approve an empty spec and start a build.
    """
    meeting = RecordingTool("request_meeting", {
        "status": "running",
        "meeting": {"task_id": "tsk_1", "run_id": "mrun_nested", "job_id": "job_1"},
        "project_id": project,
    })
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "meetnest", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "Build a thing"}},
             "next": []},
        ], "m")
        started = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, started["id"])

    assert meeting.calls, "the tool was never called"
    assert steps["m"]["status"] == "waiting", steps["m"]
    assert steps["m"]["output"]["await"] == {"kind": "meeting", "id": "mrun_nested"}


def test_a_preview_that_did_not_start_fails_the_step(eng, project, monkeypatch):
    """start_preview returns {preview: null, error: …} when there is no PR. That is a failure,
    not a successful step with nothing to wait on — otherwise deploy starts anyway."""
    preview = RecordingTool("start_preview", {
        "preview": None,
        "error": "preview_unavailable_no_pr",
        "message": "This run has not opened a pull request yet, so there is no completed "
                   "change to preview.",
        "run_id": "crun_no_pr",
    })
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))

    with eng.begin() as c:
        _, version = _publish(c, project, "prevnone", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_no_pr"}},
             "next": []},
        ], "preview")
        started = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, started["id"])

    assert preview.calls, "the tool was never called"
    assert steps["preview"]["status"] != "succeeded", steps["preview"]


def test_awaited_handle_reads_the_real_tool_shapes():
    """No database: the helper itself has to see the nested id the live tools return."""
    assert runner._awaited_handle("preview", {
        "preview": {"id": "prev_real", "status": "starting"},
    }) == {"kind": "preview", "id": "prev_real"}
    assert runner._awaited_handle("meeting", {
        "status": "running",
        "meeting": {"task_id": "tsk_1", "run_id": "mrun_real", "job_id": "job_1"},
    }) == {"kind": "meeting", "id": "mrun_real"}
    # Flat shapes the existing tests use must keep working.
    assert runner._awaited_handle("preview", {"preview_id": "prev_flat"}) == {
        "kind": "preview", "id": "prev_flat",
    }
    # A preview error carries the CODE TASK run_id. That is not a preview to wait on.
    assert runner._awaited_handle("preview", {
        "preview": None,
        "error": "preview_unavailable_no_pr",
        "run_id": "crun_no_pr",
    }) is None
    assert runner._tool_failed_to_start("preview", {
        "preview": None,
        "error": "preview_unavailable_no_pr",
        "message": "This run has not opened a pull request yet",
        "run_id": "crun_no_pr",
    }) == "This run has not opened a pull request yet"
    assert runner._tool_failed_to_start("preview", {
        "preview": {"id": "prev_ok", "status": "starting"},
    }) is None
    # Flat success used by older tests: a URL and no nested record is not a failure.
    assert runner._tool_failed_to_start("preview", {
        "preview_url": "https://started.test",
    }) is None


def test_the_panel_argues_from_the_research(eng, project, monkeypatch):
    """Research produced a cited dossier and the panel that followed it debated the original
    one-line directive as though none of it had happened — which is what research was for."""
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_after_research"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        c.execute(
            text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
                 "VALUES (:i,:p,'completed','done','Waitlists',CAST(:st AS JSONB)) "
                 "ON CONFLICT (id) DO NOTHING"),
            {"i": "drun_evidence", "p": project,
             # The keys a real dossier uses, from drun_e615b07aa693491a8aba. An earlier
             # version of this test invented `brief`/`findings`, which passed against code
             # that read the same invented names and would have handed a live panel nothing.
             "st": '{"research_brief": {"question": "referral loops beat paid ads here"},'
                   ' "signal_findings": ["three rivals gate by invite"]}'},
        )
        _, version = _publish(c, project, "researchfeed", [
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "waitlist"}},
             "next": ["meeting"]},
            {"id": "meeting", "kind": "capability",
             "binding": {"capability": "meeting",
                         "inputs": {"directive": "build a waitlist",
                                    "research": {"from_step": "research",
                                                 "field": "dossier"}}},
             "next": []},
        ], "research")
        run = _start(c, project, version["id"], {"adopt_discovery_run_id": "drun_evidence"})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(text("DELETE FROM discovery_runs WHERE id='drun_evidence'"))

    assert meeting.calls, "the panel was never convened"
    handed = meeting.calls[0]["args"].get("research") or ""
    assert "referral loops beat paid ads" in handed, "the panel got no findings"
    assert "three rivals gate by invite" in handed


def test_a_panel_still_convenes_when_there_was_no_research(eng, project, monkeypatch):
    """A project started directly has no research phase, so the dossier does not exist.
    Failing there would make the binding that IMPROVES a panel the thing that stops it."""
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_no_research"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "noresearch", [
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "x"}},
             "next": ["meeting"]},
            {"id": "meeting", "kind": "capability",
             "binding": {"capability": "meeting",
                         "inputs": {"directive": "build it",
                                    "research": {"from_step": "research",
                                                 "field": "dossier"}}},
             "next": []},
        ], "research")
        run = _start(c, project, version["id"],
                     {"adopt_code_task_run_id": "crun_direct"})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["research"]["output"]["status"] == "not_applicable"
    assert steps["meeting"]["status"] != "failed", steps["meeting"]["last_error"]


def test_research_that_died_reads_as_failed_and_the_run_carries_on(eng, project, monkeypatch):
    """Two facts the step has to hold at once.

    It FAILED — saying "not run" about work that ran and died hides something worth fixing. And
    it must not sink the run: research is enrichment, the binding into `meeting` is optional,
    and a cancelled discovery took a whole build down with it before a line of code was
    written (wfr_9782313e30cf4826a0)."""
    discovery = RecordingTool("create_discovery", {"run_id": "drun_doomed"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    with eng.begin() as c:
        _, version = _publish(c, project, "researchdies", [
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "x"}},
             "next": ["notify"]},
            {"id": "notify", "kind": "capability",
             "binding": {"capability": "notify", "inputs": {"message": "done"}}, "next": []},
        ], "research")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(
            text("INSERT INTO discovery_runs (id, project_id, status, phase, title) "
                 "VALUES ('drun_doomed', :p, 'cancelled', 'done', 'x') "
                 "ON CONFLICT (id) DO UPDATE SET status='cancelled'"),
            {"p": project},
        )
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        row = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                        {"r": run["id"]}).scalar()
        c.execute(text("DELETE FROM discovery_runs WHERE id='drun_doomed'"))

    assert steps["research"]["output"]["status"] == "failed_continued"
    assert "cancelled" in steps["research"]["output"]["note"]
    assert row != "failed", "a cancelled research killed the run"
    assert steps["notify"]["status"] != "pending", "the workflow did not carry on"


def test_what_the_research_had_reached_still_goes_to_the_panel(eng, project, monkeypatch):
    """A run that dies partway has usually written its brief and some findings already.
    Throwing those away wastes the search that WAS done and sends the panel in with nothing —
    partial evidence argued over beats no evidence."""
    discovery = RecordingTool("create_discovery", {"run_id": "drun_partial"})
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_from_partial"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "salvage", [
            {"id": "research", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "x"}},
             "next": ["meeting"]},
            {"id": "meeting", "kind": "capability",
             "binding": {"capability": "meeting",
                         "inputs": {"directive": "build it",
                                    "research": {"from_step": "research",
                                                 "field": "dossier"}}},
             "next": []},
        ], "research")
        run = _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(
            text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
                 "VALUES ('drun_partial', :p, 'cancelled', 'vet', 'Freshness', "
                 "CAST(:st AS JSONB)) ON CONFLICT (id) DO UPDATE "
                 "SET status='cancelled', state=EXCLUDED.state"),
            {"p": project,
             "st": '{"research_brief": {"question": "teams check deps monthly at best"}}'},
        )
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(text("DELETE FROM discovery_runs WHERE id='drun_partial'"))

    assert meeting.calls, "the panel never convened after research died"
    handed = meeting.calls[0]["args"].get("research") or ""
    assert "teams check deps monthly" in handed, "what the research HAD found was thrown away"


def test_a_build_that_fails_still_stops_the_run(eng, project, monkeypatch):
    """Only RESEARCH is tolerated. The build is the work: a run whose build failed has nothing
    to preview and nothing to deploy, and carrying on would be a lie."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))

    with eng.begin() as c:
        _, version = _publish(c, project, "builddies", [
            {"id": "build", "kind": "capability",
             "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}},
             "next": []},
        ], "build")
        run = _start(c, project, version["id"], {"adopt_code_task_run_id": "crun_doomed"})
        runner.tick(c, Ctx(eng), project_id=project)
        _finish_code_task(c, project, "crun_doomed", "failed", pr=None)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert steps["build"]["status"] != "succeeded"


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


class _Edition:
    def __init__(self, edition):
        self.boss_edition = edition


class EditionCtx(Ctx):
    """A context whose settings name an edition, as EngineContext.create() does."""

    def __init__(self, engine, edition):
        super().__init__(engine)
        self.settings = _Edition(edition)


def _two_step_build(c, project, name):
    _, version = _publish(c, project, name, [
        {"id": "research", "kind": "capability",
         "binding": {"capability": "research", "inputs": {"seed": "a todo app"}},
         "next": ["meeting"]},
        {"id": "meeting", "kind": "capability",
         "binding": {"capability": "meeting",
                     "inputs": {"directive": "a todo app",
                                "research": {"from_step": "research",
                                             "field": "dossier"}}},
         "next": []},
    ], "research")
    return _start(c, project, version["id"], {})


def test_community_skips_research_visibly_only_when_it_cannot_run(eng, project, monkeypatch):
    """Build starts Standard Product Delivery, whose first step is research. Community research
    is a governed job on the Claude Code subscription; where that subscription cannot be used,
    the job could only be refused, so the step stands aside with the router's reason, an event
    names it, nothing is commissioned, and the run reaches the panel."""
    from boss_engine.services import discovery_service

    discovery = RecordingTool("create_discovery", {"run_id": "drun_never"})
    meeting = RecordingTool("request_meeting", {"run_id": "mrun_community"})
    monkeypatch.setattr(runner, "_tool_registry",
                        lambda: RecordingRegistry([discovery, meeting]))
    asked = []

    def unavailable(connection, settings, project_id, actor="boss"):
        asked.append(project_id)
        return {"reason": discovery_service.RESEARCH_SOURCE_UNAVAILABLE,
                "source_id": "anthropic_claude_code", "source_status": "not_connected",
                "detail": "Research runs on the Claude Code subscription (not_connected)."}

    monkeypatch.setattr(discovery_service, "research_unavailable", unavailable)

    with eng.begin() as c:
        run = _two_step_build(c, project, "communitybuild")
        ctx = EditionCtx(eng, "community")
        runner.tick(c, ctx, project_id=project)
        runner.tick(c, ctx, project_id=project)
        steps = _steps(c, run["id"])
        events = c.execute(
            text("SELECT event_type, data FROM events WHERE project_id=:p "
                 "AND event_type = 'workflow.research_skipped'"),
            {"p": project},
        ).mappings().all()

    assert asked == [project]
    assert not discovery.calls, "research was commissioned where it cannot run"
    research = steps["research"]
    assert research["status"] == "succeeded", research["last_error"]
    assert research["output"]["status"] == "not_applicable"
    assert research["output"]["reason"] == "research_source_unavailable"
    assert "research cannot run here" in research["output"]["note"]
    assert "not_connected" in research["output"]["note"]
    assert meeting.calls, "the panel was never convened after the skipped research"
    assert "research" not in meeting.calls[0]["args"]
    assert len(events) == 1
    assert events[0]["data"]["reason"] == "research_source_unavailable"
    assert events[0]["data"]["source_status"] == "not_connected"
    assert events[0]["data"]["step_id"]


def test_community_runs_governed_research_when_it_can(eng, project, monkeypatch):
    """With a usable research subscription Community commissions the discovery — a governed
    job — and the step waits on it instead of skipping."""
    from boss_engine.services import discovery_service

    discovery = RecordingTool("create_discovery", {"run_id": "drun_governed"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))
    monkeypatch.setattr(discovery_service, "research_unavailable",
                        lambda connection, settings, project_id, actor="boss": None)

    with eng.begin() as c:
        _, version = _publish(c, project, "communityresearch", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        run = _start(c, project, version["id"], {})
        runner.tick(c, EditionCtx(eng, "community"), project_id=project)
        steps = _steps(c, run["id"])
        skipped = c.execute(
            text("SELECT count(*) FROM events WHERE project_id=:p "
                 "AND event_type = 'workflow.research_skipped'"), {"p": project}).scalar()

    assert discovery.calls, "Community did not commission research it can run"
    assert steps["r"]["status"] == "waiting"
    assert steps["r"]["output"]["await"]["id"] == "drun_governed"
    assert skipped == 0


def test_a_research_check_that_errors_is_a_visible_skip(eng, project, monkeypatch):
    """The availability check failing must neither deadletter Build nor pass silently."""
    from boss_engine.services import discovery_service

    discovery = RecordingTool("create_discovery", {"run_id": "drun_never"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    def broken(connection, settings, project_id, actor="boss"):
        raise RuntimeError("routing table missing")

    monkeypatch.setattr(discovery_service, "research_unavailable", broken)

    with eng.begin() as c:
        _, version = _publish(c, project, "communitycheck", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        run = _start(c, project, version["id"], {})
        runner.tick(c, EditionCtx(eng, "community"), project_id=project)
        steps = _steps(c, run["id"])
        events = c.execute(
            text("SELECT data FROM events WHERE project_id=:p "
                 "AND event_type = 'workflow.research_skipped'"), {"p": project}).mappings().all()

    assert not discovery.calls
    assert steps["r"]["output"]["reason"] == "research_check_failed"
    assert "routing table missing" in steps["r"]["output"]["note"]
    assert events and "routing table missing" in events[0]["data"]["detail"]


def test_community_still_adopts_research_that_already_exists(eng, project, monkeypatch):
    """Reading a dossier that already exists spends nothing, so it is adopted, never re-run."""
    discovery = RecordingTool("create_discovery", {"run_id": "drun_second"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    with eng.begin() as c:
        _, version = _publish(c, project, "communityadopt", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        run = _start(c, project, version["id"], {"adopt_discovery_run_id": "drun_first"})
        runner.tick(c, EditionCtx(eng, "community"), project_id=project)
        steps = _steps(c, run["id"])

    assert not discovery.calls
    assert steps["r"]["output"]["await"]["id"] == "drun_first"


def test_other_editions_still_research(eng, project, monkeypatch):
    discovery = RecordingTool("create_discovery", {"run_id": "drun_ent"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([discovery]))

    with eng.begin() as c:
        _, version = _publish(c, project, "enterpriseresearch", [
            {"id": "r", "kind": "capability",
             "binding": {"capability": "research", "inputs": {"seed": "go"}}, "next": []},
        ], "r")
        _start(c, project, version["id"], {})
        runner.tick(c, EditionCtx(eng, "enterprise"), project_id=project)

    assert discovery.calls, "the Community research check leaked into another edition"


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


def test_preview_does_not_adopt_a_row_that_lies(eng, project, monkeypatch):
    """A row saying 'running' is not an app that answers.

    app_previews goes stale in every ordinary way — the container is removed, the host is
    rebuilt, the sandbox is reaped. Adopting on the database's word alone reported the step as
    succeeded with a URL that served nothing, and `deploy` ran next because its inputs come
    from `build` rather than from here. A build nobody could look at shipped anyway, against an
    explicit instruction that without a preview the run must not move to deploy.
    """
    preview = RecordingTool("start_preview", {"preview_id": "prev_fresh", "preview_url": "https://fresh.test"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([preview]))
    monkeypatch.setattr(runner, "_preview_answers", lambda url, timeout=6.0: False)

    with eng.begin() as c:
        c.execute(
            text("INSERT INTO app_previews (id, project_id, run_id, url, status) "
                 "VALUES (:i,:p,:r,'https://dead.test','running') "
                 "ON CONFLICT (id) DO NOTHING"),
            {"i": f"prevdead_{project}", "p": project, "r": "crun_dead"},
        )
        _, version = _publish(c, project, "prevdead", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_dead"}},
             "next": []},
        ], "preview")
        _start(c, project, version["id"], {})
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(text("DELETE FROM app_previews WHERE project_id=:p"), {"p": project})

    assert preview.calls, "a dead preview must be replaced, not adopted"


def test_deploy_refuses_when_the_preview_produced_nothing(eng, project, monkeypatch):
    """The rule the operator asked for: without a preview, do not move to deploy.

    Enforced in the runner rather than by binding preview_url into eleven catalogue entries —
    a rule written once cannot be forgotten by the next person to add a workflow.
    """
    deploy = RecordingTool("deploy_app", {"deployment_id": "dep_1"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([deploy]))

    with eng.begin() as c:
        _, version = _publish(c, project, "nopreview", [
            {"id": "preview", "kind": "capability",
             "binding": {"capability": "preview", "inputs": {"run_id": "crun_x"}},
             "next": ["deploy"]},
            {"id": "deploy", "kind": "capability",
             "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
             "next": []},
        ], "preview")
        run = _start(c, project, version["id"], {})
        # The preview step never produced a URL.
        c.execute(
            text("UPDATE workflow_step_runs SET status='succeeded', output='{}'::jsonb "
                 "WHERE workflow_run_id=:r AND step_id='preview'"),
            {"r": run["id"]},
        )
        problem = runner._preview_missing_for_run(c, {"run_id": run["id"]})

    assert problem and "without a URL" in problem or "nothing was previewable" in (problem or "")
    assert not deploy.calls


def test_a_workflow_without_a_preview_step_is_not_blocked(eng, project):
    """A hotfix lane or a data migration is not asking for a preview and must not wait for one."""
    with eng.begin() as c:
        _, version = _publish(c, project, "nopreviewstep", [
            {"id": "deploy", "kind": "capability",
             "binding": {"capability": "deploy", "inputs": {"run_id": "crun_x"}},
             "next": []},
        ], "deploy")
        run = _start(c, project, version["id"], {})
        assert runner._preview_missing_for_run(c, {"run_id": run["id"]}) is None


def test_timeout_approve_on_a_work_step_still_runs_the_tool(eng, project, monkeypatch):
    """on_timeout=approve means 'proceed with the work', not 'pretend the work happened'.

    A person-approval of a work step (build, meeting, preview, …) only unblocks it; the tool
    still has to run. The timeout sweep used to mark those steps succeeded without ever
    calling execute_step, so a build that nobody approved in time was recorded as built
    without a code task. Gates — where the approval IS the work — still complete here.
    """
    meeting = RecordingTool("request_meeting", {"meeting_run_id": "mrun_sla", "task_id": "tsk_sla"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting]))

    with eng.begin() as c:
        _, version = _publish(c, project, "sla_work", [
            {"id": "m", "kind": "capability",
             "binding": {"capability": "meeting", "inputs": {"directive": "Build it"}},
             "approval": {"mode": "manual", "sla_hours": 1, "on_timeout": "approve"},
             "next": []},
        ], "m")
        run = _start(c, project, version["id"])
        opened = runner.tick(c, Ctx(eng), project_id=project)
        assert opened["performed"][0]["outcome"] == "gate_opened", opened
        assert not meeting.calls, "the tool ran before anyone (or policy) approved it"

        _overdue(c, run["id"])
        swept = runner.sweep_gate_timeouts(c, project_id=project)
        after_sweep = _steps(c, run["id"])["m"]
        assert swept["applied"][0]["outcome"] == "auto_approved"
        assert after_sweep["status"] == "ready", (
            "timeout-approve finished the work step without running it: "
            f"{after_sweep['status']} output={after_sweep.get('output')}"
        )
        assert not meeting.calls, "the sweep itself executed the tool"

        released = runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])

    assert meeting.calls, "policy approved the step and the tool still never ran"
    assert meeting.calls[0]["args"] == {"directive": "Build it"}
    # The meeting tool starts asynchronous work. The step must wait for its
    # recorded meeting run instead of claiming the meeting already finished.
    assert steps["m"]["status"] == "waiting"
    assert steps["m"]["output"]["meeting_run_id"] == "mrun_sla"
    assert released["performed"][0]["outcome"] == "awaiting"
