"""ONE GATE, ONE RUN: a decision that gates a run's step belongs to exactly that run and step.

Two runs of one workflow reached the same Approval gate (`action: resume_workflow`) and got ONE
card: create_blocking_decision merges an open decision with the same question and task, and the
gate's question is the step's words, the same for every run. One Approve released both runs.
The same failure had been fixed for governance gates alone (a per-run question in the capability
runner) — the decision step, a parked step and a stack deploy's gate still collided.

The class is closed in two places, and this file fails if either is undone:
  * run_decisions.open_step_decision is THE one way a step's decision is opened. It writes the
    run and step into the recommendation and keeps the question distinct in words.
  * create_blocking_decision counts the run and step as part of the cause it merges on, so even
    two identical questions from two runs stay two decisions.
And the scan below fails on any producer that raises a step's decision any other way.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from boss_engine.services import core, run_decisions

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("engine/boss_engine", "apps/api/app", "apps/worker")
PRODUCERS = {"create_blocking_decision", "create_blocking"}
#: The sinks and the one helper. Only the helper may raise a decision that names a run's step.
ALLOWED = {
    ("engine/boss_engine/services/core.py", "create_blocking_decision"),
    ("engine/boss_engine/services/core.py", "_create_blocking_decision_postgres"),
    ("engine/boss_engine/defaults/events.py", "create_blocking"),
    ("engine/boss_engine/services/run_decisions.py", "open_step_decision"),
}
#: What marks a decision as a step's gate in the code that builds it.
RUN_IDENTITY_KEYS = {"workflow_run_id", "step_id"}
RESUME_ACTION = "resume_workflow"


def _functions():
    for base in SCAN_ROOTS:
        root = ROOT / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if "tests" in path.relative_to(ROOT).parts:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield rel, node, source


def _calls(func, names):
    return [n for n in ast.walk(func) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) in names or getattr(n.func, "attr", None) in names)]


def _recommendation_exprs(call: ast.Call, func) -> list[ast.AST]:
    """The expression(s) a producer call passes as its recommendation (5th argument), followed
    through a local variable to every value assigned to it in the same function."""
    arg = next((kw.value for kw in call.keywords if kw.arg == "recommendation"), None)
    if arg is None and len(call.args) > 4:
        arg = call.args[4]
    if arg is None:
        return []
    if isinstance(arg, ast.Name):
        found = []
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == arg.id for t in node.targets):
                found.append(node.value)
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == arg.id and isinstance(node.ctx, ast.Store)):
                found.append(node.slice)
        return found
    return [arg]


def _names_a_step(expr: ast.AST) -> bool:
    """Does this recommendation name a run's step, or ask to resume one?"""
    for node in ast.walk(expr):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in RUN_IDENTITY_KEYS:
                    return True
        if isinstance(node, ast.Constant) and node.value in RUN_IDENTITY_KEYS | {RESUME_ACTION}:
            return True
    return False


def _links_a_step_to_a_decision(func, source) -> bool:
    """Does this function point a workflow step at a decision (not clear one)?"""
    for node in ast.walk(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            sql = " ".join(node.value.split()).lower()
            if ("update workflow_step_runs" in sql and "decision_id" in sql
                    and "decision_id = null" not in sql and "decision_id=null" not in sql):
                return True
    return False


def test_the_scan_sees_the_producers():
    seen = {(rel, f.name) for rel, f, _ in _functions() if _calls(f, PRODUCERS)}
    assert len(seen) > 30, "the scan no longer finds the producers"
    helper = {(rel, f.name) for rel, f, _ in _functions() if _calls(f, {"open_step_decision"})}
    for where in (("engine/boss_engine/services/workflows.py", "_process_control_step"),
                  ("engine/boss_engine/services/workflow_capability_runner.py", "_open_gate"),
                  ("engine/boss_engine/services/workflow_capability_runner.py",
                   "_park_for_decision"),
                  ("engine/boss_engine/services/stack_deploy.py", "plan_stack_deploy")):
        assert where in helper, f"{where} no longer opens its step's decision through the helper"


def test_no_step_decision_is_raised_except_through_open_step_decision():
    offenders = []
    for rel, func, _ in _functions():
        if (rel, func.name) in ALLOWED:
            continue
        for call in _calls(func, PRODUCERS):
            if any(_names_a_step(e) for e in _recommendation_exprs(call, func)):
                offenders.append(f"{rel}:{call.lineno} {func.name}")
    assert not offenders, (
        "these raise a decision that names a workflow run's step with create_blocking_decision "
        "directly. Two runs asking the same question then share ONE card and one Approve "
        "releases both. Use run_decisions.open_step_decision:\n  " + "\n  ".join(offenders))


#: Steps that SHOW a decision another producer owns, without being released by it. A deploy
#: waiting for its build's merge points at the PR's merge question so the step reads Needs you,
#: and marks `released_for` so the answered-gate sweep never runs the step on that approval:
#: the observed merge releases it. Every run waiting on that PR's merge is meant to go on then.
DISPLAY_ONLY = {("engine/boss_engine/services/workflow_capability_runner.py", "_follow_merge")}


def test_every_step_that_waits_on_a_decision_got_it_from_open_step_decision():
    offenders = []
    for rel, func, source in _functions():
        if not _links_a_step_to_a_decision(func, source):
            continue
        if (rel, func.name) in DISPLAY_ONLY:
            assert "released_for" in ast.get_source_segment(source, func), (
                f"{rel} {func.name} shows another producer's decision on a step; it must mark "
                "the step released_for that decision so its approval never releases the step")
            continue
        if _calls(func, PRODUCERS) or not _calls(func, {"open_step_decision"}):
            offenders.append(f"{rel}:{func.lineno} {func.name}")
    assert not offenders, (
        "these point a workflow step at a decision that open_step_decision did not open, so "
        "another run's step can end up waiting on the same card:\n  " + "\n  ".join(offenders))


# ---- the helper and the merge, behaviourally -------------------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Decisions:
    """Pending decisions in memory, answering the two lookups the helper and the sink make."""

    def __init__(self):
        self.rows: list[dict] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "INSERT INTO decisions" in sql:
            self.rows.append({"id": params["id"], "question": params["question"],
                              "task_id": params["task_id"],
                              "recommendation": json.loads(params["recommendation"])})
            return _Result(None)
        if sql.lstrip().startswith("SELECT") and "FROM decisions" in sql:
            question = params.get("q", params.get("question"))
            for row in reversed(self.rows):
                rec = row["recommendation"]
                if row["question"] != question:
                    continue
                if "gate_run" in params:
                    if rec.get("workflow_run_id") != params["gate_run"]:
                        continue
                    if params["gate_run"] is not None and rec.get("step_id") != params["gate_step"]:
                        continue
                return _Result({"id": row["id"], "recommendation": rec,
                                "run_id": rec.get("workflow_run_id"),
                                "step_id": rec.get("step_id")})
            return _Result(None)
        return _Result(None)


@pytest.fixture
def decisions(monkeypatch):
    monkeypatch.setattr("boss_engine.ports.get_engine_ports", lambda: None)
    monkeypatch.setattr(core, "_emit_event_postgres", lambda *a, **k: None)
    return _Decisions()


def _open(c, run_id, step_id="look", question="Go ahead with it?"):
    return run_decisions.open_step_decision(
        c, "p1", None, question, {"action": RESUME_ACTION}, {}, "waiting",
        run_id=run_id, step_id=step_id)


def test_two_runs_at_one_gate_get_two_decisions(decisions):
    first = _open(decisions, "wfr_aaaaaaaaaaaaaaaa")
    second = _open(decisions, "wfr_bbbbbbbbbbbbbbbb")
    assert first != second
    owners = {r["id"]: r["recommendation"]["workflow_run_id"] for r in decisions.rows}
    assert owners == {first: "wfr_aaaaaaaaaaaaaaaa", second: "wfr_bbbbbbbbbbbbbbbb"}
    # Told apart in words on the Approvals page, without a raw id.
    assert [r["question"] for r in decisions.rows] == ["Go ahead with it?", "Go ahead with it? (2)"]


def test_the_same_runs_same_step_still_folds_into_its_open_card(decisions):
    assert _open(decisions, "wfr_aaaaaaaaaaaaaaaa") == _open(decisions, "wfr_aaaaaaaaaaaaaaaa")


def test_two_steps_of_one_run_asking_the_same_words_are_two_decisions(decisions):
    assert _open(decisions, "wfr_a", "one") != _open(decisions, "wfr_a", "two")


def test_the_merge_itself_tells_two_runs_apart_even_with_the_same_words(decisions):
    """Defence in depth: a producer that got the words the same still gets two rows."""
    def raw(run_id):
        return core._create_blocking_decision_postgres(
            decisions, "p1", None, "Same words", {"workflow_run_id": run_id, "step_id": "s"},
            {}, "r")
    assert raw("wfr_a") != raw("wfr_b")
    # An alert with no run identity still collapses (one open decision per cause).
    alert = lambda: core._create_blocking_decision_postgres(decisions, "p1", None, "Alert", {}, {}, "r")  # noqa: E731
    assert alert() == alert()


def test_the_run_identity_cannot_be_overridden_by_the_spec(decisions):
    decision_id = run_decisions.open_step_decision(
        decisions, "p1", None, "Q", {"workflow_run_id": "wfr_other", "step_id": "other"}, {},
        "r", run_id="wfr_mine", step_id="mine")
    rec = next(r for r in decisions.rows if r["id"] == decision_id)["recommendation"]
    assert (rec["workflow_run_id"], rec["step_id"]) == ("wfr_mine", "mine")


def test_a_step_decision_without_its_run_is_refused():
    with pytest.raises(ValueError):
        run_decisions.open_step_decision(None, "p1", None, "Q", {}, {}, "r", run_id="", step_id="s")


def test_the_merge_key_names_the_run_and_the_step():
    source = inspect.getsource(core._create_blocking_decision_postgres)
    assert "recommendation->>'workflow_run_id' IS NOT DISTINCT FROM :gate_run" in source
    assert "recommendation->>'step_id' IS NOT DISTINCT FROM :gate_step" in source
