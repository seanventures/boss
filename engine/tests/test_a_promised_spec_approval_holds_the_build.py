"""A workflow that promises "you approve the spec before any code" holds its build until you do.

THE CASE (laptop, feat/monitoring 1df1cbf16, 2026-10-19). Add-a-feature run wfr_723213d8, Minor
Feature v6 (meeting → build → …), on the Order API. The start form said "Approve the spec — You
read the spec before any code" — /asks lists a workflow's meeting as the spec ask when it has no
spec_review step, and with BOSS_MEETING_PRD_AUTO_APPROVE off that ask is the person's. Then:

- the panel finished and its PRD decision dec_eb911c56 stayed PENDING;
- the meeting step went green the moment the panel finished, and build crun_e18a3a67 started;
- approving dec_eb911c56 would have started a SECOND build of the same spec
  (decisions._start_code_task_after_prd), racing the first.

The fix is one source for the promise and the behaviour (workflow_asks.spec_ask_step_ids):

1. a meeting step that carries the spec ask waits on its PRD decision before anything after it;
2. a PRD decision whose panel a building workflow convened never starts a build of its own;
3. a run that already went past its spec (started before 1.) marks the card, which then offers
   only Acknowledge or Reject.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import decision_actions, spec_gate
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_asks import asks_for_spec, holds_for_spec, spec_ask_step_ids

from .test_a_failed_step_stops_the_run import _insert_awaited
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    RecordingTool,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

PRD = "# PRD: GET /orders/summary\n\nToday's order count and revenue by status."


@pytest.fixture(autouse=True)
def _clean(eng, project):
    """The rows these tests add that the shared project teardown does not remove. Depends on
    `project`, so it is torn down first."""
    yield
    with eng.begin() as c:
        for table in ("meeting_runs", "code_task_runs", "events", "decisions", "tasks"):
            with c.begin_nested():
                c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                          {"p": project})


def _minor_feature(c, project_id: str, build_id: str):
    """Minor Feature's shape: a panel, then a build, and no spec_review step."""
    _, version = _publish(c, project_id, f"minor_{uuid.uuid4().hex[:6]}", [
        {"id": "meeting", "kind": "capability",
         "binding": {"capability": "meeting", "inputs": {"directive": "Add GET /orders/summary"}},
         "next": ["build"]},
        {"id": "build", "kind": "capability",
         "binding": {"capability": "build",
                     "inputs": {"title": "Orders summary", "instructions": "build it"}},
         "next": []},
    ], "meeting")
    return _start(c, project_id, version["id"], {})


def _panel_finished(c, project_id: str, run_id: str, meeting_id: str,
                    decision_status: str = "pending") -> str:
    """The panel the meeting step convened finishes, raising its PRD decision, as
    meeting_service._finalize_meeting does."""
    step_run_id = c.execute(text(
        "SELECT id FROM workflow_step_runs WHERE workflow_run_id = :r AND step_id = 'meeting'"),
        {"r": run_id}).scalar()
    task_id = f"tsk_{uuid.uuid4().hex[:10]}"
    _insert_awaited(c, project_id, "tasks", task_id, "open")
    decision_id = f"dec_{uuid.uuid4().hex[:10]}"
    _insert_awaited(c, project_id, "decisions", decision_id, decision_status)
    c.execute(text("UPDATE decisions SET task_id = :t, risk_tier = 2, recommendation = "
                   "CAST(:rec AS JSONB) WHERE id = :d"),
              {"d": decision_id, "t": task_id, "rec": json.dumps({
                  "action": "execute_agent_tool", "tool": "publish_document",
                  "start_code_task": True, "reason_code": "meeting_prd_ready_to_publish",
                  "args": {"title": "PRD: Orders summary", "markdown": PRD,
                           "repo_subdir": "docs/prd"}})})
    _insert_awaited(c, project_id, "meeting_runs", meeting_id, "completed")
    c.execute(text("UPDATE meeting_runs SET decision_id = :d, task_id = :t, "
                   "state = CAST(:s AS JSONB), finalize_ctx = CAST(:f AS JSONB) WHERE id = :m"),
              {"m": meeting_id, "d": decision_id, "t": task_id,
               "s": json.dumps({"prd_markdown": PRD}),
               "f": json.dumps({"origin": {"actor": "test", "step_run_id": step_run_id}})})
    return decision_id


def _tools(monkeypatch, meeting_id: str, build_id: str):
    meeting = RecordingTool("request_meeting", {"meeting_run_id": meeting_id})
    build = RecordingTool("create_code_task", {"run_id": build_id})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting, build]))
    return meeting, build


def _ids():
    return f"mrun_{uuid.uuid4().hex[:10]}", f"crun_{uuid.uuid4().hex[:10]}"


# ── 1. One source: the steps that promise the spec are the steps that hold for it ───────────

def test_the_promise_and_the_hold_come_from_one_list():
    minor = [{"id": "meeting", "binding": {"capability": "meeting"}},
             {"id": "build", "binding": {"capability": "build"}}]
    reviewed = [{"id": "meeting", "binding": {"capability": "meeting"}},
                {"id": "spec_review", "binding": {"capability": "spec_review"}},
                {"id": "build", "binding": {"capability": "build"}}]
    asked = {a["step_id"] for a in asks_for_spec({"entry_step": "meeting", "steps": minor},
                                                 prd_auto_approve=False) if a["kind"] == "spec"}
    assert asked == set(spec_ask_step_ids(minor)) == {"meeting"}
    assert holds_for_spec(minor, "meeting")
    # With a spec_review step, that step is the ask (it waits on the same decision itself), so
    # the panel's step does not hold as well.
    assert spec_ask_step_ids(reviewed) == {"spec_review"}
    assert not holds_for_spec(reviewed, "meeting")
    assert not holds_for_spec(minor, "build")


# ── 1. The meeting step waits on the PRD decision ───────────────────────────────────────────

def test_a_finished_panel_does_not_start_the_build_until_its_spec_is_approved(
        eng, project, monkeypatch):
    meeting_id, build_id = _ids()
    _meeting, build = _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["meeting"]["status"] == "waiting"
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        # THE BUG: the meeting step went green here and the build started.
        assert steps["meeting"]["status"] == "waiting", steps["meeting"]
        assert steps["meeting"]["output"]["await"]["kind"] == runner.SPEC_DECISION_KIND
        assert steps["meeting"]["output"]["await"]["id"] == decision_id
        assert steps["build"]["status"] == "pending", steps["build"]
        assert not build.calls, "the build started before the spec was approved"

        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo' "
                       "WHERE id = :d"), {"d": decision_id})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["meeting"]["status"] == "succeeded", steps["meeting"]
        # The build is briefed from the approved spec, and knows who approved it.
        assert steps["meeting"]["output"]["prd_markdown"].startswith("# PRD")
        assert steps["meeting"]["output"]["spec_approved_by"] == "ceo"
        assert len(build.calls) == 1


def test_a_rejected_spec_stops_the_run_and_cannot_be_skipped(eng, project, monkeypatch):
    meeting_id, build_id = _ids()
    _meeting, build = _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        runner.tick(c, Ctx(eng), project_id=project)
        c.execute(text("UPDATE decisions SET status = 'rejected' WHERE id = :d"),
                  {"d": decision_id})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["meeting"]["status"] == "failed", steps["meeting"]
        assert "spec was not approved" in steps["meeting"]["last_error"]
        assert steps["meeting"]["output"].get(runner.REJECTED_BY_APPROVER) is True
        assert not build.calls


def test_an_already_approved_spec_does_not_hold(eng, project, monkeypatch):
    """Auto-approval (or a person who was quick) approves the decision before the panel's run is
    marked completed: the step goes straight on, as before."""
    meeting_id, build_id = _ids()
    _meeting, build = _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, run["id"], meeting_id, decision_status="approved")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["meeting"]["status"] == "succeeded"
        assert len(build.calls) == 1


# ── 2. Approving the PRD never starts a second build ────────────────────────────────────────

def _approve_follow_on(c, decision_id: str, monkeypatch) -> list[dict]:
    """Run the approval's follow-on with everything that would start a build recorded."""
    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service, decisions, repo_registry

    started: list[dict] = []
    monkeypatch.setattr(repo_registry, "resolve_repository_target",
                        lambda *a, **k: {"owner": "acme", "repo": "orders"})
    monkeypatch.setattr(decisions.threading, "Thread",
                        lambda target=None, **k: type("T", (), {
                            "start": lambda self: started.append({"via": "decomposed"})})())
    monkeypatch.setattr(code_task_service, "start_followup_task",
                        lambda *a, **k: started.append({"via": "followup"}) or {"run_id": "x"})
    row = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": decision_id}).mappings().one())
    decisions.DecisionService(get_settings())._start_code_task_after_prd(
        c, row, "ceo", row["recommendation"])
    return started


def test_approving_a_prd_its_workflow_already_builds_starts_no_second_build(
        eng, project, monkeypatch):
    meeting_id, build_id = _ids()
    _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        # The build the workflow already started (wfr_723213d8's crun_e18a3a67).
        _insert_awaited(c, project, "code_task_runs", build_id, "running")
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) WHERE id = :i"),
                  {"i": build_id, "f": json.dumps({"workflow_run_id": run["id"]})})
        assert spec_gate.workflow_build_for_prd(c, decision_id)["build"]["id"] == build_id
        started = _approve_follow_on(c, decision_id, monkeypatch)
        assert started == [], f"approving the PRD started another build: {started}"
        said = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                              "event_type = 'code_task.already_started' AND entity_id = :b"),
                         {"p": project, "b": build_id}).scalar()
        assert said and said["workflow_run_id"] == run["id"]


def test_approving_a_prd_onto_an_app_its_workflow_builds_starts_no_followup(
        eng, project, monkeypatch):
    """The Add-a-feature shape: the PRD names the app it extends, which goes down the follow-up
    path (decisions._build_prd_onto_app) — the second build on the same branch."""
    meeting_id, build_id = _ids()
    _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        c.execute(text("UPDATE decisions SET recommendation = recommendation || "
                       "'{\"extends_run_id\": \"crun_parent\"}'::jsonb WHERE id = :d"),
                  {"d": decision_id})
        # The app it extends exists (the Order API's own build).
        from boss_engine.services import code_task_service

        monkeypatch.setattr(code_task_service, "load_code_task_run",
                            lambda _c, rid: {"id": rid, "project_id": project})
        monkeypatch.setattr(code_task_service, "is_simulated_run", lambda _r: False)
        assert _approve_follow_on(c, decision_id, monkeypatch) == []


def test_a_panel_convened_outside_a_workflow_still_builds_on_approval(eng, project, monkeypatch):
    """The panel path: no workflow step convened it, so its approval is what starts the build."""
    meeting_id, build_id = _ids()
    _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        c.execute(text("UPDATE meeting_runs SET finalize_ctx = '{}'::jsonb WHERE id = :m"),
                  {"m": meeting_id})
        assert spec_gate.workflow_build_for_prd(c, decision_id) is None
        assert _approve_follow_on(c, decision_id, monkeypatch) == [{"via": "decomposed"}]


# ── 3. A run that already went past its spec: the card says so ──────────────────────────────

def test_a_build_running_past_an_open_spec_marks_its_card(eng, project, monkeypatch):
    meeting_id, build_id = _ids()
    _tools(monkeypatch, meeting_id, build_id)
    with eng.begin() as c:
        run = _minor_feature(c, project, build_id)
        runner.tick(c, Ctx(eng), project_id=project)
        # As wfr_723213d8 did before the hold: the panel finished with no decision to hold on
        # yet, the step went green and the build started; the PRD decision is open beside it.
        _insert_awaited(c, project, "meeting_runs", meeting_id, "completed")
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        assert _steps(c, run["id"])["build"]["status"] == "waiting"
        c.execute(text("DELETE FROM meeting_runs WHERE id = :m"), {"m": meeting_id})
        decision_id = _panel_finished(c, project, run["id"], meeting_id)
        _insert_awaited(c, project, "code_task_runs", build_id, "running")
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) WHERE id = :i"),
                  {"i": build_id, "f": json.dumps({"workflow_run_id": run["id"]})})
        c.execute(text("UPDATE workflow_step_runs SET output = output - 'spec_gate_checked' "
                       "WHERE workflow_run_id = :r"), {"r": run["id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        row = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                             {"d": decision_id}).mappings().one())
        assert row["status"] == "pending", "the card is marked, never answered for the person"
        mark = row["recommendation"].get(spec_gate.SKIPPED_KEY) or {}
        assert mark.get("build_run_id") == build_id, row["recommendation"]
        offered = decision_actions.allowed_actions(row)
        assert [(a["action"], a["label"]) for a in offered] == [
            ("approve", "Acknowledge"), ("reject", "Reject")]
        assert build_id in offered[0]["effect"] and "Starts nothing" in offered[0]["effect"]
