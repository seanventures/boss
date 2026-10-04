"""An agent step ends with a STRUCTURED outcome, and the run believes it — not the loop ending.

THE INCIDENT (the bundled "Video edit and publish" example, run live). The `edit` step's agent
called `media.fetch_link` on a YouTube watch page, which is HTML, not a video. It made no edit,
rendered nothing, and said so: "I could not complete this task ... No EditPlan, RenderedVideo, or
RenderDefectReport was produced ... nothing was published." The agent LOOP had finished, so the
step's status was `completed`; the step went green, and the run opened the `publish` step's
approval gate — asking a person to approve uploading a video that did not exist.

The rules this file holds every agent step to (services/agent_steps.py `verdict`, applied by
workflow_capability_runner `_judged_agent_step`):

1. The agent declares completed | blocked | failed. failed or blocked FAILS the step and stops
   the run for Retry / Skip, with the agent's one line on the run card and its full report kept.
   blocked also says what is needed.
2. A product a later step needs (derived from the wiring, or declared as `binding.requires`)
   that is missing fails the step with an explained reason, whatever the agent declared.
3. A downstream gate never opens over a failed upstream step.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import agent_steps
from boss_engine.services import workflow_capability_runner as runner

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    _editor,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

#: What the agent said on the live run, near enough, followed by the declaration it is now asked
#: to end with.
COULD_NOT = (
    "I could not complete this task.\n\nWhat happened: media.fetch_link failed — the link is a "
    "YouTube watch page, not a video file. No EditPlan, RenderedVideo, or RenderDefectReport was "
    "produced, and nothing was published."
)


def _said(prose: str, **declared) -> str:
    return prose + ("\n" + json.dumps(declared) if declared else "")


def _agent_said(summary: str, **produced) -> dict:
    """What `agent_steps.run` hands back: the loop ended (`completed`), whatever the work did."""
    return {"status": "completed", "summary": summary, "agent": "video_editor",
            "agent_run_id": f"arun_{uuid.uuid4().hex[:8]}",
            "agent_outcome": agent_steps.declared_outcome(summary), **produced}


# ── The verdict, on its own ──────────────────────────────────────────────────────────────────

def test_a_declared_failure_fails_with_the_agents_reason():
    judged = agent_steps.verdict(
        _agent_said(_said(COULD_NOT, outcome="failed",
                          reason="the link is a YouTube page, not a video file")),
        step_name="Edit")
    assert judged["outcome"] == "failed"
    assert judged["error"] == ("Edit could not be completed: the link is a YouTube page, "
                               "not a video file")


def test_blocked_says_what_is_needed():
    judged = agent_steps.verdict(_agent_said(_said(
        "I need a different link.", outcome="blocked", reason="the link is a web page",
        needs="a Google Drive share link or a direct .mp4 link")), step_name="Edit")
    assert judged["outcome"] == "blocked"
    assert "needs: a Google Drive share link or a direct .mp4 link" in judged["error"]


def test_completed_without_a_required_product_fails_and_says_which():
    judged = agent_steps.verdict(
        _agent_said(_said("Done.", outcome="completed", reason="edited as asked")),
        required=["media_artifact_id"], step_name="Edit")
    assert judged["outcome"] == "failed"
    assert judged["missing"] == ["media_artifact_id"]
    assert judged["error"] == ("Edit produced no media artifact (media_artifact_id): "
                               "edited as asked")


def test_an_undeclared_outcome_is_judged_on_its_products_not_its_prose():
    """No declaration: the decision is the structured check, never a string match. The prose
    supplies the one-line reason only."""
    missing = agent_steps.verdict(_agent_said(COULD_NOT), required=["media_artifact_id"],
                                  step_name="Edit")
    assert missing["outcome"] == "failed" and not missing["declared"]
    assert missing["error"].endswith(": I could not complete this task.")
    present = agent_steps.verdict(_agent_said("All done.", media_artifact_id="art_1"),
                                  required=["media_artifact_id"], step_name="Edit")
    assert present["outcome"] == "completed"


def test_the_agent_is_always_asked_to_declare_how_it_ended():
    import inspect

    assert "OUTCOME_ASK" in inspect.getsource(agent_steps.run)
    for word in ("completed", "blocked", "failed", "needs"):
        assert word in agent_steps.OUTCOME_ASK


# ── The run: a failed agent step stops it, and the publish gate never opens ──────────────────

def _edit_then_publish(c, project_id, requires=None):
    """edit (agent) → publish, which waits for a person and reads the edit's video — the
    shape of the video example. `publish` is a manual-approval step, so if it so much as gets a
    decision, a gate opened over the edit."""
    _editor(c, project_id)
    binding = {"agent_work": True, "inputs": {"instructions": "trim it"},
               "produces": ["title"]}
    if requires:
        binding["requires"] = requires
    _, version = _publish(c, project_id, f"edit_{uuid.uuid4().hex[:6]}", [
        {"id": "edit", "kind": "capability", "captain": "video_editor",
         "binding": binding, "next": ["publish"]},
        {"id": "publish", "kind": "capability",
         "binding": {"capability": "notify", "inputs": {
             "message": {"from_step": "edit", "field": "media_artifact_id"},
             # Overridable from the run's input, so NOT something the edit must produce.
             "subject": {"from_step": "edit", "field": "title",
                         "override_from_input": "title"}}},
         "approval": {"mode": "manual"}, "next": []},
    ], "edit")
    return _start(c, project_id, version["id"], {})


def _decisions_for(c, run_id: str, step_id: str) -> int:
    return int(c.execute(
        text("SELECT count(*) FROM workflow_step_runs WHERE workflow_run_id=:r "
             "AND step_id=:s AND decision_id IS NOT NULL"), {"r": run_id, "s": step_id}).scalar())


def _run_status(c, run_id: str) -> str:
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


def _drive(eng, project, monkeypatch, said: dict, requires=None):
    calls: list[dict] = []

    def fake_run(*_a, **k):
        calls.append(k)
        return dict(said)

    monkeypatch.setattr(agent_steps, "run", fake_run)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    c = eng.connect()
    tx = c.begin()
    run = _edit_then_publish(c, project, requires)
    for _ in range(3):
        runner.tick(c, Ctx(eng), project_id=project)
    return c, tx, run, calls


def _assert_stopped_before_publish(c, run, calls):
    from boss_engine.services.workflow_run_actions import (
        RETRYABLE_STEP_STATES, SKIPPABLE_STEP_STATES, STOPPED_RUN_STATES)

    steps = _steps(c, run["id"])
    # SETTLED: stopped once (a settled failure is `deadlettered`), not re-run on its own — the
    # agent would only say the same thing again, at the price of a whole agent run.
    assert steps["edit"]["status"] == "deadlettered", steps["edit"]
    assert len(calls) == 1
    # ...and the operator's two ways forward are offered on it.
    assert steps["edit"]["status"] in RETRYABLE_STEP_STATES & SKIPPABLE_STEP_STATES
    assert _run_status(c, run["id"]) in STOPPED_RUN_STATES
    assert steps["publish"]["status"] == "pending", steps["publish"]
    assert _decisions_for(c, run["id"], "publish") == 0, "a publish gate opened over a failure"
    return steps


def test_an_agent_that_reports_failure_stops_the_run_and_no_gate_opens(eng, project,
                                                                        monkeypatch):
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(_said(
        COULD_NOT, outcome="failed", reason="the link is a YouTube page, not a video file")))
    try:
        steps = _assert_stopped_before_publish(c, run, calls)
        assert steps["edit"]["last_error"] == (
            "Edit could not be completed: the link is a YouTube page, not a video file")
        # The agent's whole explanation is kept beside the one line.
        assert steps["edit"]["output"]["summary"].startswith("I could not complete this task")
        assert steps["edit"]["output"]["agent_outcome"]["outcome"] == "failed"
    finally:
        tx.rollback()
        c.close()


def test_the_live_incident_undeclared_could_not_with_no_video_stops_the_run(eng, project,
                                                                            monkeypatch):
    """Exactly what the live run returned: prose only, and no video. The wiring says publish
    needs `media_artifact_id`, so the edit fails — naming the missing video."""
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(COULD_NOT))
    try:
        steps = _assert_stopped_before_publish(c, run, calls)
        assert steps["edit"]["last_error"] == (
            "Edit produced no media artifact (media_artifact_id): "
            "I could not complete this task.")
    finally:
        tx.rollback()
        c.close()


def test_completed_but_the_required_product_is_missing_fails_with_the_reason(eng, project,
                                                                            monkeypatch):
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(
        _said("Trimmed it.", outcome="completed", reason="trimmed 0:28 to 1:48"),
        title="A title"))
    try:
        steps = _assert_stopped_before_publish(c, run, calls)
        assert steps["edit"]["last_error"] == (
            "Edit produced no media artifact (media_artifact_id): trimmed 0:28 to 1:48")
    finally:
        tx.rollback()
        c.close()


def test_a_product_the_step_declares_it_requires_is_checked_too(eng, project, monkeypatch):
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(
        _said("Done.", outcome="completed", reason="done"), media_artifact_id="art_1"),
        requires=["rendered_video_url"])
    try:
        steps = _assert_stopped_before_publish(c, run, calls)
        assert "produced no rendered video url (rendered_video_url)" in steps["edit"]["last_error"]
    finally:
        tx.rollback()
        c.close()


def test_blocked_stops_the_run_and_says_what_is_needed(eng, project, monkeypatch):
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(_said(
        "That link is a web page.", outcome="blocked", reason="the link is a YouTube page",
        needs="a Google Drive share link or a direct .mp4 link")))
    try:
        steps = _assert_stopped_before_publish(c, run, calls)
        assert steps["edit"]["last_error"] == (
            "Edit is blocked: the link is a YouTube page — needs: a Google Drive share link "
            "or a direct .mp4 link")
    finally:
        tx.rollback()
        c.close()


def test_an_agent_that_completed_with_its_product_succeeds_and_the_gate_opens(eng, project,
                                                                              monkeypatch):
    c, tx, run, calls = _drive(eng, project, monkeypatch, _agent_said(
        _said("Trimmed and rendered.", outcome="completed", reason="trimmed as asked"),
        media_artifact_id="art_rendered", title="A title"))
    try:
        steps = _steps(c, run["id"])
        assert steps["edit"]["status"] == "succeeded", steps["edit"]
        assert steps["edit"]["output"]["agent_outcome"]["outcome"] == "completed"
        assert _decisions_for(c, run["id"], "publish") == 1, steps.get("publish")
    finally:
        tx.rollback()
        c.close()


def test_an_agent_that_finished_after_an_approval_is_judged_the_same_way(eng, project,
                                                                         monkeypatch):
    """The other way an agent step ends: it stopped for a person's approval, and its run
    finished later. `completed` there is the loop ending too."""
    awaited = f"arun_{uuid.uuid4().hex[:8]}"
    c, tx, run, calls = _drive(eng, project, monkeypatch,
                               {"status": "suspended", "agent_run_id": awaited})
    try:
        assert _steps(c, run["id"])["edit"]["status"] == "waiting"
        c.execute(text(
            "INSERT INTO agent_runs (id, project_id, status, messages, transcript, usage, "
            "output_text) VALUES (:i, :p, 'completed', '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, "
            ":o)"), {"i": awaited, "p": project,
                     "o": _said(COULD_NOT, outcome="failed", reason="nothing was rendered")})
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
        assert steps["edit"]["status"] == "failed", steps["edit"]
        assert steps["edit"]["last_error"] == "Edit could not be completed: nothing was rendered"
        assert steps["publish"]["status"] == "pending"
        assert _decisions_for(c, run["id"], "publish") == 0
    finally:
        tx.rollback()
        c.close()


@pytest.mark.parametrize("wired,required", [
    ({"from_step": "edit", "field": "media_artifact_id"}, True),
    ({"from_step": "edit", "field": "title", "override_from_input": "title"}, False),
])
def test_what_a_step_must_produce_is_read_off_the_wiring(eng, project, wired, required):
    steps = {
        "edit": {"id": "edit", "binding": {"agent_work": True}},
        "publish": {"id": "publish", "binding": {"capability": "notify",
                                                  "inputs": {"message": wired}}},
    }
    with eng.connect() as c:
        got = runner.required_products(c, project, steps["edit"], steps)
    assert (wired["field"] in got) is required
