"""A budget refusal that clears by itself WAITS; one that needs a person STOPS, and says why.

Found installing the five bundled examples on a live install (2026-10-03): five runs started
within seconds, the budget was green ($7.03 of $10 available, nothing reserved), and "Meeting to
tickets" dead-lettered at its first step with "'Read the meeting' was denied: budget denied:
denied". The refusal was the per-minute burst cap — the other runs had spent the minute — and
it would have cleared by itself a few seconds later. Three things were wrong, each a class:

1. the skill runtime reported every budget refusal as "budget denied: denied", so nothing
   downstream could tell a refusal that clears by itself from one only a person can lift;
2. the workflow runner failed the step for both, so a one-attempt step dead-lettered its run
   on a refusal that needed nothing but a minute;
3. a single reservation larger than the per-minute cap could never be granted, empty window or
   not — discovery and the connection proof each clamped their own estimate to dodge it, and
   every other caller was simply unservable on a project with a low cap.

These tests drive the real BudgetGovernor and the real SkillRuntimeService through the real
runner; only the skill's own effect (a model call) is replaced.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services import workflows
from boss_engine.services.budget import BudgetGovernor
from boss_engine.services.model_failures import explain

SKILL = "meeting.extract_actions"
HOLDER = "program_manager"
LABEL = "Read the meeting"


@pytest.fixture()
def eng(engine):
    """The API suite's engine: a schema this run owns, converged to schema.sql and seeded."""
    return engine


def _policy(c, project_id: str, *, daily: int, burst: int) -> None:
    c.execute(text("DELETE FROM budget_policies WHERE scope_type='project' AND scope_id=:p"),
              {"p": project_id})
    c.execute(
        text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
             "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
             "VALUES (:i, 'project', :p, :d, :m, :b)"),
        {"i": f"bp_{uuid.uuid4().hex[:10]}", "p": project_id, "d": daily, "m": daily * 20,
         "b": burst},
    )


def _spent_this_minute(c, project_id: str, amount: int) -> None:
    """Another run's reservation, made a few seconds ago."""
    c.execute(
        text("INSERT INTO budget_reservations (id, project_id, category, amount_usd_micros, "
             "reason, status, expires_at) VALUES (:i, :p, 'model', :a, 'another run', 'active', "
             "now() + interval '10 minutes')"),
        {"i": f"res_{uuid.uuid4().hex[:12]}", "p": project_id, "a": amount},
    )


def _a_minute_passes(c, project_id: str, run_id: str | None = None) -> None:
    c.execute(text("UPDATE budget_reservations SET created_at = created_at - interval '2 minutes' "
                   "WHERE project_id = :p"), {"p": project_id})
    if run_id:
        c.execute(text("UPDATE workflow_step_runs SET run_after = now() "
                       "WHERE workflow_run_id = :r AND status = 'ready'"), {"r": run_id})


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:   # an empty test database: make the org and workspace a project needs
            org, wid = f"org_{pid}", f"ws_{pid}"
            c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'budget wait test')"),
                      {"i": org})
            c.execute(text("INSERT INTO workspaces (id, org_id, name) "
                           "VALUES (:i, :o, 'budget wait test')"), {"i": wid, "o": org})
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'budget wait test', :s)"),
            {"i": pid, "w": wid, "s": pid},
        )
    yield pid
    with eng.begin() as c:
        for table in ("workflow_step_runs", "workflow_runs", "workflow_custom_steps",
                      "workflow_mappings", "workflow_versions", "workflow_definitions",
                      "skill_invocations", "budget_reservations", "deadletters",
                      "project_skills", "decisions", "events", "receipts", "role_skills",
                      "roles", "policy_eval_log"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this schema lacks
                pass
        c.execute(text("DELETE FROM budget_policies WHERE scope_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


class Ctx:
    def __init__(self, engine):
        self._engine = engine
        self.settings = Settings()

    def begin(self):
        return self._engine.begin()


@pytest.fixture()
def governed_skill(monkeypatch):
    """The real SkillRuntimeService — permission, the budget reservation, the record — with only
    the model call replaced. Counts the calls that reached the effect."""
    from boss_engine.services import agent_loop
    from boss_engine.services.skill_runtime import SkillRuntimeService

    effects: list[dict[str, Any]] = []

    def invoke(connection, settings, project_id, skill_id, caller, args, *, actor, **_):
        runtime = SkillRuntimeService(settings)
        spec = runtime.entitlements.get_skill(skill_id, project_id, connection=connection)
        return runtime.invoke(
            connection, project_id, skill_id, caller, args, mock=False,
            executor=lambda inputs: effects.append(dict(inputs)) or {
                "actions": [], "decisions": [], "unclear": [], "summary": "ok",
                "action_count": 0},
            skill_spec=spec, actor=actor)

    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", invoke)
    return effects


def _meeting_run(c, project_id: str, *, max_attempts: int = 1) -> dict[str, Any]:
    c.execute(
        text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
             "VALUES (:i, :p, :r, :r, '{}'::jsonb) ON CONFLICT (project_id, role_key) DO NOTHING"),
        {"i": f"role_{uuid.uuid4().hex[:10]}", "p": project_id, "r": HOLDER})
    c.execute(
        text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
             "VALUES (:i, :p, :r, :s, 'builtin') ON CONFLICT DO NOTHING"),
        {"i": f"rs_{uuid.uuid4().hex[:10]}", "p": project_id, "r": HOLDER, "s": SKILL})
    custom_steps.create(c, project_id=project_id, label=LABEL, binding_kind="skill",
                        skill_id=SKILL, actor="test", step_key="extract")
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key="meeting_to_tickets", name="Meeting to tickets",
        description=None, actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": "extract", "steps": [
            {"id": "extract", "kind": "capability", "next": [], "agent": HOLDER,
             "approval": {"mode": "auto"},
             "max_attempts": max_attempts,
             "binding": {"custom_step": "extract",
                         "inputs": {"transcript": "Ana will send the deck by Friday."}}},
        ]})
    return workflows.start_run(c, project_id=project_id, actor="test", input_data={},
                               dedupe_key=f"dk_{uuid.uuid4().hex[:10]}",
                               workflow_version_id=version["id"])


def _state(c, run_id: str) -> dict[str, Any]:
    step = c.execute(
        text("SELECT status, attempt, last_error, output, run_after FROM workflow_step_runs "
             "WHERE workflow_run_id = :r"), {"r": run_id}).mappings().one()
    run = c.execute(text("SELECT status, blocked_reason FROM workflow_runs WHERE id = :r"),
                    {"r": run_id}).mappings().one()
    dead = c.execute(text("SELECT count(*) FROM deadletters WHERE source_id IN "
                          "(SELECT id FROM workflow_step_runs WHERE workflow_run_id = :r)"),
                     {"r": run_id}).scalar_one()
    return {"step": dict(step), "run": dict(run), "deadletters": dead}


# ── The governor ────────────────────────────────────────────────────────────────────────────

def test_one_reservation_larger_than_the_per_minute_cap_is_granted_when_the_minute_is_empty(
        eng, project):
    """The cap limits a RATE. Refusing one reservation larger than it, with nothing else spent,
    refused it for ever — waiting never makes an estimate smaller."""
    with eng.begin() as c:
        _policy(c, project, daily=10_000_000, burst=1_000)
        granted = BudgetGovernor(Settings()).reserve(
            c, project, category="model", amount_usd_micros=50_000, reason="big step",
            expires_in_minutes=5)
        after = BudgetGovernor(Settings()).reserve(
            c, project, category="model", amount_usd_micros=1, reason="next",
            expires_in_minutes=5)
    assert granted["status"] == "reserved", granted
    assert after["status"] == "denied" and after["refusal"] == "burst", (
        "the cap still governs what comes after the first reservation")


def test_a_per_minute_refusal_says_it_clears_by_itself_and_when(eng, project):
    with eng.begin() as c:
        _policy(c, project, daily=10_000_000, burst=100_000)
        _spent_this_minute(c, project, 99_500)
        refused = BudgetGovernor(Settings()).reserve(
            c, project, category="model", amount_usd_micros=1_000, reason="sixth run",
            expires_in_minutes=5)
    assert refused["status"] == "denied"
    assert refused["refusal"] == "burst" and refused["transient"] is True
    assert 5 <= refused["retry_after_seconds"] <= 60
    assert explain(refused["message"])["kind"] == "budget_wait"


def test_a_cap_refusal_says_a_person_has_to_act(eng, project):
    with eng.begin() as c:
        _policy(c, project, daily=1_000, burst=100_000)
        refused = BudgetGovernor(Settings()).reserve(
            c, project, category="model", amount_usd_micros=5_000, reason="too big",
            expires_in_minutes=5)
    assert refused["refusal"] == "cap_exceeded" and refused["transient"] is False
    assert explain(refused["message"])["kind"] == "budget_refused"


@pytest.mark.parametrize("message", [
    "Tier-2+ work is frozen at this budget utilization. Decision created.",
    "Budget allocation exceeded. Decision created.",
])
def test_every_refusal_a_person_must_lift_is_explained_as_one(message):
    said = explain(message)
    assert said["kind"] == "budget_refused" and said["retry"] is False


# ── A step the per-minute budget refused ────────────────────────────────────────────────────

def test_a_step_refused_by_the_per_minute_budget_waits_then_completes(
        eng, project, governed_skill):
    """The incident, end to end: other runs spent the minute, the first step is refused. It
    must wait — not fail, not use up its one attempt, not dead-letter — say so on the run, and
    complete on a later tick once the minute has moved on."""
    with eng.begin() as c:
        _policy(c, project, daily=10_000_000, burst=100_000)
        _spent_this_minute(c, project, 100_000)
        run = _meeting_run(c, project, max_attempts=1)
        runner.tick(c, Ctx(eng), project_id=project)
        waiting = _state(c, run["id"])

    assert governed_skill == [], "nothing ran while the budget refused it"
    assert waiting["deadletters"] == 0
    assert waiting["step"]["status"] == "ready", waiting
    assert waiting["step"]["attempt"] == 0, "a wait is not an attempt"
    assert waiting["step"]["output"]["waiting"]["kind"] == "budget_wait"
    assert waiting["run"]["status"] == "waiting"
    assert "per-minute budget" in (waiting["run"]["blocked_reason"] or ""), waiting["run"]
    assert LABEL in waiting["run"]["blocked_reason"]

    with eng.begin() as c:
        runner.tick(c, Ctx(eng), project_id=project)   # still inside the wait: not claimed
        assert _state(c, run["id"])["step"]["status"] == "ready"
        assert governed_skill == []
        _a_minute_passes(c, project, run["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        done = _state(c, run["id"])

    assert len(governed_skill) == 1
    assert done["step"]["status"] == "succeeded", done
    assert done["deadletters"] == 0
    assert done["run"]["blocked_reason"] is None, "the wait's sentence went with the wait"


def test_a_step_larger_than_the_per_minute_cap_runs_when_nothing_else_spent_the_minute(
        eng, project, governed_skill):
    with eng.begin() as c:
        _policy(c, project, daily=10_000_000, burst=1)    # below any estimate
        run = _meeting_run(c, project)
        runner.tick(c, Ctx(eng), project_id=project)
        state = _state(c, run["id"])
    assert state["step"]["status"] == "succeeded", state
    assert len(governed_skill) == 1


# ── A step a cap refused ────────────────────────────────────────────────────────────────────

def test_a_step_refused_by_the_cap_stops_once_with_the_reason_and_the_action(
        eng, project, governed_skill):
    """Waiting cannot lift a cap, and retrying spends ticks to reach the same sentence. The step
    stops at once — even with retries left — with what happened and what to do, and the run
    offers Try again / Skip like every other stopped step."""
    with eng.begin() as c:
        _policy(c, project, daily=100, burst=100_000)     # below any estimate
        run = _meeting_run(c, project, max_attempts=3)
        runner.tick(c, Ctx(eng), project_id=project)
        state = _state(c, run["id"])

    assert governed_skill == []
    assert state["step"]["status"] in workflows.STOPPED_STEP_STATES, state
    error = state["step"]["last_error"] or ""
    assert "budget denied: denied" not in error
    assert error.startswith(f"'{LABEL}' could not start: The budget refused this work"), error
    assert "budget cap" in error and "Raise the daily or monthly cap in Budget" in error
    assert "Decision created" not in error, "that decision is rolled back with the step"
    assert state["run"]["status"] in {"failed", "deadlettered"}
    assert (state["run"]["blocked_reason"] or "").startswith(f"Stopped at {LABEL}: "), (
        "the run names the step as the workflow does, not by its id")
    assert state["run"]["blocked_reason"].endswith(
        "Try it again, or skip it and carry on."), state["run"]


def test_a_skill_refused_by_the_budget_says_which_refusal_to_any_caller(eng, project):
    """The chat agent calls the same runtime. Its refusal names the reason and carries
    whether it clears by itself."""
    from boss_engine.services.skill_runtime import SkillRuntimeService

    with eng.begin() as c:
        _policy(c, project, daily=10_000_000, burst=100_000)
        _spent_this_minute(c, project, 100_000)
        runtime = SkillRuntimeService(Settings())
        spec = runtime.entitlements.get_skill(SKILL, project, connection=c)
        result = runtime.invoke(c, project, SKILL, HOLDER, {"transcript": "x"}, mock=False,
                                executor=lambda _: {}, skill_spec=spec, actor="test")
    assert result["status"] == "denied"
    assert result["reason"] == "budget refused: Burst cap exceeded ($/min). Retry shortly."
    assert result["budget"]["transient"] is True and result["budget"]["refusal"] == "burst"
