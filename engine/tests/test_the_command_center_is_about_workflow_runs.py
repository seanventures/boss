"""The Command Center answers what is running, what needs me, and how recent work went — in runs.

It was built when work meant `tasks` and kept reading them after the product moved to
workflows. A run started by Build creates no task, so the thing an operator had just started
never appeared on the landing page; "Live coordination" read `tasks.owner_role_id`, which
nothing populates, and animated idle orbs; "Pending decisions" listed rows that linked nowhere.
These tests pin the replacement (boss_engine.services.command_center) without a database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services import command_center as cc

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

SPEC = {"steps": [
    {"id": "research", "binding": {"capability": "research"}},
    {"id": "build", "binding": {"capability": "build"}, "captain": "release_manager"},
    {"id": "helper", "binding": {"agent_work": True}},
    {"id": "deploy", "binding": {"capability": "deploy"}},
]}


def run(run_id: str, status: str, *, age=timedelta(minutes=30), **extra) -> dict:
    return {"id": run_id, "status": status, "workflow_key": "standard_delivery",
            "workflow_name": "Standard Product Delivery", "spec": SPEC,
            "input": {"title": "Incident timeline builder"},
            "created_at": NOW - age, "updated_at": NOW - age / 2, "completed_at": None,
            "blocked_reason": None, "error": None, **extra}


def step(run_id: str, step_id: str, status: str, **extra) -> dict:
    return {"workflow_run_id": run_id, "step_id": step_id, "status": status,
            "step_kind": "capability", "output": {}, "last_error": None,
            "decision_id": None, "started_at": NOW - timedelta(minutes=12),
            "completed_at": None, **extra}


def build(**kw) -> dict:
    base = dict(project_id="p", runs=[], steps=[], decisions=[], readiness=[],
                names={"release_manager": "Release Manager"}, now=NOW)
    base.update(kw)
    return cc.assemble(**base)


def test_a_run_started_by_build_is_running_with_its_step_agent_and_elapsed_time():
    out = build(runs=[run("wfr_1", "running")],
                steps=[step("wfr_1", "research", "succeeded"),
                       step("wfr_1", "build", "running")])
    [row] = out["running"]
    assert row["run_id"] == "wfr_1"
    assert row["subject"] == "Incident timeline builder"
    assert row["step_label"] == "Build, review and test"
    # The named captain, by display name — not the raw role key.
    assert row["agent"] == {"role_key": "release_manager", "name": "Release Manager",
                            "how": "named"}
    assert row["elapsed_seconds"] == 12 * 60
    assert (row["steps_done"], row["step_count"]) == (1, 4)


def test_an_agent_step_says_who_boss_picked_or_that_it_will_pick():
    picked = build(runs=[run("wfr_1", "running")],
                   steps=[step("wfr_1", "helper", "running", output={"agent": "qa"})])
    assert picked["running"][0]["agent"]["how"] == "picked"
    waiting = build(runs=[run("wfr_1", "running")], steps=[step("wfr_1", "helper", "ready")])
    assert waiting["running"][0]["agent"]["how"] == "auto"


def test_a_step_with_no_named_captain_shows_the_capabilitys_own():
    out = build(runs=[run("wfr_1", "running")], steps=[step("wfr_1", "deploy", "running")])
    assert out["running"][0]["agent"]["how"] == "default"
    assert out["running"][0]["agent"]["role_key"] == "deployment_manager"


def test_a_gate_on_a_run_needs_you_and_links_to_both_the_decision_and_the_run():
    out = build(
        runs=[run("wfr_1", "waiting")],
        steps=[step("wfr_1", "deploy", "waiting", decision_id="dec_1")],
        decisions=[{"id": "dec_1", "status": "pending", "question": "q", "risk_tier": 2,
                    "recommendation": {"workflow_run_id": "wfr_1", "capability": "deploy"},
                    "created_at": NOW}])
    [item] = out["needs_you"]
    assert item["kind"] == "approval" and item["decision_kind"] == "deploy_gate"
    assert item["decision_id"] == "dec_1" and item["run_id"] == "wfr_1"
    assert item["actions"] == ["approve", "reject", "delegate"]
    assert "Deploy" in item["title"]


def test_a_failed_step_on_a_stopped_run_needs_you_with_retry_and_the_reason():
    out = build(runs=[run("wfr_1", "failed")],
                steps=[step("wfr_1", "deploy", "failed", last_error="no deployment target\n…")])
    [item] = out["needs_you"]
    assert item["kind"] == "failed_step"
    # The run's OWN answers (workflow_run_actions.stopped_for_operator), not a Retry the
    # Command Center decided on: integration of this stream with main's 0fd6c45.
    assert item["actions"] == ["retry_step", "skip_step"]
    assert all(a["enabled"] and a["step_id"] == "deploy" for a in item["answers"])
    assert item["detail"] == "no deployment target"
    assert item["what_to_do"]


def test_a_refused_approval_is_not_offered_as_a_step_to_retry():
    """THE INTEGRATION INCIDENT. This list used its own status sets and offered Retry on every
    failed step — including an approval somebody answered no to, where Try again would turn a
    person's no into another attempt and the run itself offers nothing. It now reads
    workflow_run_actions.stopped_for_operator, the rule the Workflow screen and the briefing
    read, so a row appears only with the answers the run really offers."""
    out = build(runs=[run("wfr_1", "failed")],
                steps=[step("wfr_1", "approve", "failed", step_kind="decision",
                            last_error="rejected by the release manager")])
    assert [n for n in out["needs_you"] if n["kind"] == "failed_step"] == []


def test_a_run_the_operator_cancelled_is_not_waiting_for_a_retry():
    out = build(runs=[run("wfr_1", "cancelled")],
                steps=[step("wfr_1", "deploy", "cancelled")])
    assert out["needs_you"] == []
    assert out["recent"][0]["outcome"] == "cancelled"


def test_a_failure_from_last_month_is_history_not_a_todo():
    out = build(runs=[run("wfr_1", "failed", age=timedelta(days=60))],
                steps=[step("wfr_1", "deploy", "failed")])
    assert out["needs_you"] == [] and out["recent"] == []


def test_a_spending_refusal_needs_you_with_the_fix_not_an_approve_button():
    out = build(decisions=[{"id": "dec_9", "status": "pending", "question": "Model capacity…",
                            "risk_tier": 1, "created_at": NOW,
                            "recommendation": {"action": "model_capacity_denied"}}])
    [item] = out["needs_you"]
    assert item["decision_kind"] == "refusal"
    assert item["actions"] == ["reject"]
    assert item["fix_area"] == "setup" and "Retry" in item["what_to_do"]


def test_a_factory_build_out_of_model_capacity_is_a_notice_after_the_real_gates():
    """The decision a Factory build raises on a rate limit or empty credit, as
    governed_code_task_service writes it. It was classed `general`, so it was ranked first as
    a gate with "Review and decide", above a deploy gate that could actually be answered."""
    out = build(
        runs=[run("wfr_2", "waiting")],
        steps=[step("wfr_2", "deploy", "waiting", decision_id="dec_1")],
        decisions=[
            {"id": "dec_8", "status": "pending", "risk_tier": 2, "created_at": NOW,
             "question": "Model capacity exhausted (TPM / rate limit / credits / budget).",
             "recommendation": {"action": "restore_model_capacity_or_raise_budget",
                                "reason_code": "model_rate_limit",
                                "reason": "model_capacity_exhausted"}},
            {"id": "dec_1", "status": "pending", "question": "gate", "risk_tier": 2,
             "created_at": NOW, "recommendation": {"workflow_run_id": "wfr_2",
                                                   "capability": "deploy"}},
        ])
    first, last = out["needs_you"]
    assert first["decision_kind"] == "deploy_gate"
    assert last["decision_kind"] == "refusal" and last["actions"] == ["reject"]
    assert last["fix_area"] == "setup" and last["fix_label"] == "Open Repo and model"


def test_an_unfinished_setup_step_is_a_readiness_gap():
    out = build(readiness=[{"id": "github", "title": "Connect a repository", "done": False},
                           {"id": "model", "title": "Connect a model", "done": True}])
    assert [n["title"] for n in out["needs_you"]] == ["Connect a repository"]
    assert out["needs_you"][0]["fix_area"] == "setup"


def test_what_stops_work_comes_first():
    out = build(
        runs=[run("wfr_1", "failed"), run("wfr_2", "waiting")],
        steps=[step("wfr_1", "deploy", "failed"),
               step("wfr_2", "deploy", "waiting", decision_id="dec_1")],
        decisions=[
            {"id": "dec_9", "status": "pending", "question": "refused", "risk_tier": 1,
             "created_at": NOW, "recommendation": {"action": "budget_denied"}},
            {"id": "dec_1", "status": "pending", "question": "gate", "risk_tier": 2,
             "created_at": NOW, "recommendation": {"workflow_run_id": "wfr_2",
                                                   "capability": "deploy"}},
        ],
        readiness=[{"title": "Connect a model", "done": False}])
    kinds = [(n["kind"], n.get("decision_kind")) for n in out["needs_you"]]
    assert kinds == [("approval", "deploy_gate"), ("failed_step", None),
                     ("readiness", None), ("approval", "refusal")]
    assert out["counts"]["needs_you"] == 4


def test_recent_outcomes_are_the_last_week_of_finished_runs_newest_first():
    out = build(runs=[run("wfr_old", "completed", age=timedelta(days=3)),
                      run("wfr_new", "completed", age=timedelta(hours=2))])
    assert [r["run_id"] for r in out["recent"]] == ["wfr_new", "wfr_old"]
    assert out["counts"]["succeeded_7d"] == 2
