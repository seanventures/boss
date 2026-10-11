"""An applied template's gates BLOCK (F2 §2.3, S2.1): when `template_tests` or
`no_committed_secrets` fails, nothing the build wrote is pushed and no PR opens or changes.

Before this, both gates were ordinary gates: a build whose template tests failed ended
`completed` with quality gate `fail` and pushed to the app's PR (ship-with-dissent), and every
build pushed checkpoints to the branch as it went, so on an apply onto an app's branch a
committed secret was on the open PR before any gate had looked at it.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import textwrap

import pytest

from boss_engine.services import code_task_service as cts
from boss_engine.services import factory_view as fv
from boss_engine.services import governed_code_task_service as gov

TEMPLATE_GATES = ("template_tests", "no_committed_secrets")


# --- the disposition: no autonomy setting ships past them -------------------------------------

@pytest.mark.parametrize("gate", TEMPLATE_GATES)
@pytest.mark.parametrize("advisory", [True, False])
@pytest.mark.parametrize("never_block", [True, False])
def test_a_failed_template_gate_is_never_shipped_past(gate, advisory, never_block):
    exhausted = gov.resolve_gate_failure_action(
        failing=[gate], advisory_gate=advisory, rounds_used=2, max_repair_rounds=2,
        never_block=never_block, faults=gov.classify_gate_faults(failing=[gate], evidence={}))
    assert exhausted == "block"
    early = gov.resolve_gate_failure_action(
        failing=[gate], advisory_gate=advisory, rounds_used=0, max_repair_rounds=2,
        never_block=never_block, faults={})
    assert early == "repair"         # a repair re-runs the build; it pushes nothing (the hold)


@pytest.mark.parametrize("gate", TEMPLATE_GATES)
def test_a_failed_template_gate_beside_a_judgement_gate_still_blocks(gate):
    action = gov.resolve_gate_failure_action(
        failing=["code_review_passed", gate], advisory_gate=True, rounds_used=3,
        max_repair_rounds=2, never_block=True,
        faults={"code_review_passed": "HARNESS", gate: "WORK"})
    assert action == "block"


def test_the_template_gates_are_mechanical_never_a_harness_fault():
    assert gov.classify_gate_faults(failing=list(TEMPLATE_GATES), evidence={}) == {
        g: "WORK" for g in TEMPLATE_GATES}


# --- the hold: nothing is pushed before the gates pass ------------------------------------------

def test_the_hold_rules():
    assert gov.template_push_hold(False, None) is None
    assert gov.template_push_hold(False, {"template_tests": False}) is None
    assert "nothing is pushed before its gates pass" in gov.template_push_hold(True, None)
    assert gov.template_push_hold(True, {}) is not None                       # never ran = held
    assert gov.template_push_hold(True, {"template_tests": True}) is not None  # one missing = held
    for gate in TEMPLATE_GATES:
        reason = gov.template_push_hold(True, {**{g: True for g in TEMPLATE_GATES}, gate: False})
        assert reason is not None and gate in reason
    assert gov.template_push_hold(True, {g: True for g in TEMPLATE_GATES}) is None


def test_unknown_counts_as_a_template_build(monkeypatch):
    @contextlib.contextmanager
    def broken(_engine):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(gov, "_transaction", broken)
    assert gov._applies_template(object(), {"id": "crun_x"}) is True   # noqa: SLF001


_PUSHES = {"upsert_file", "ensure_branch", "propose_pull_request", "create_pull_request"}


def _guarded(node: ast.AST, parents: dict) -> bool:
    """Is a `_push_held(...)` call evaluated before `node` on every path to it: in an earlier
    statement of an enclosing block, or in the condition of an enclosing `if`?"""
    child = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.If) and child is not parent.test and _calls_hold(parent.test):
            return True
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and child in block:
                if any(_calls_hold(stmt) for stmt in block[:block.index(child)]):
                    return True
        if isinstance(parent, ast.FunctionDef) and parent.name == "_run_governed_code_task":
            return False
        child, parent = parent, parents.get(parent)
    return False


def _calls_hold(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_push_held"
               for n in ast.walk(tree))


def test_every_push_of_the_build_waits_for_the_hold():
    """THE CLASS: every place the governed build writes to the remote (stage commits,
    checkpoints, the repair hand-off, the partial-PR rescue, the declared app, the PR) is
    preceded by `_push_held`. A new push site added without it fails here."""
    source = textwrap.dedent(inspect.getsource(gov._run_governed_code_task))  # noqa: SLF001
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    pushes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr in _PUSHES]
    assert len(pushes) >= 8, "the scan no longer sees the build's pushes"
    unguarded = [f"line {n.lineno}: {n.func.attr}" for n in pushes if not _guarded(n, parents)]
    assert not unguarded, unguarded


def test_a_blocked_secret_leaves_no_diff_to_ship_from():
    source = inspect.getsource(gov._run_governed_code_task)  # noqa: SLF001
    block = source[source.index('elif action == "block":'):]
    block = block[:block.index("_rescue_pr(_blocked_reason)")]
    assert 'if "no_committed_secrets" in failing:' in block and "pending = []" in block


# --- no waiver opens a PR past them -------------------------------------------------------------

@pytest.mark.parametrize("gate", TEMPLATE_GATES)
def test_a_waiver_cannot_open_the_pr_past_a_template_gate(gate, monkeypatch):
    run = {"id": "crun_t", "project_id": "proj_t", "title": "apply hello",
           "finalize_ctx": {"failing_gates": [gate], "pending_diff": [{"path": "a.py", "content": "x"}]}}
    monkeypatch.setattr(cts, "_transaction", lambda _e: contextlib.nullcontext(None))
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _rid: run)
    monkeypatch.setattr(cts, "SoftwareFactoryService",
                        lambda *a, **k: pytest.fail("a PR was about to be opened"))
    with pytest.raises(ValueError, match="can't be waived"):
        cts.override_code_task(None, None, None, "crun_t", waive_gates=[gate], actor="owner")
    with pytest.raises(ValueError, match="can't be waived"):
        cts.override_code_task(None, None, None, "crun_t", waive_gates=["*"], actor="owner")


def test_the_console_says_why_they_cannot_be_waived():
    for gate in TEMPLATE_GATES:
        assert gate in fv.UNWAIVABLE_GATES and gate in fv.UNWAIVABLE_WHY
        assert gate in fv.GATE_LABELS
    build = fv.attention_build({"run_id": "crun_t", "failing": ["no_committed_secrets"]})
    assert build["failing"][0]["waivable"] is False
    assert build["failing"][0]["reason"] == "Can't be waived: a committed secret never reaches a pull request"
