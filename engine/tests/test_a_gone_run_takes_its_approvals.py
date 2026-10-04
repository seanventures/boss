"""Every path that ends a run for good withdraws the decisions it left unanswered.

boss-1, 2026-10-04: runs stopped and then removed (both 200) left 21 gates in Approvals with
Approve and Reject on them. `run_decisions.withdraw_with_run` is the one place that closes them;
this file fails on any code in engine/ or apps/ that closes or deletes a workflow run without
calling it — the next "end a run" path cannot be added and forget the questions it raised.

The behaviour end to end, through the endpoints the console uses, is
apps/api/tests/test_a_gone_run_takes_its_approvals.py.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from boss_engine.services import decision_actions as da
from boss_engine.services import run_decisions as rd
from boss_engine.services.workflow_run_actions import CLOSED_RUN_STATES

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app",
           ROOT / "apps" / "worker" / "boss_worker"]

#: SQL that ends a run for good: it is set to a closed state, or deleted.
_ENDS_A_RUN = re.compile(
    r"UPDATE\s+workflow_runs\s+SET\s+status\s*=\s*'(?:"
    + "|".join(sorted(CLOSED_RUN_STATES)) + r")'"
    r"|DELETE\s+FROM\s+workflow_runs\b",
    re.IGNORECASE,
)


def _functions_that_end_a_run() -> list[tuple[str, str, bool]]:
    found = []
    for base in SOURCES:
        for path in sorted(base.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if not _ENDS_A_RUN.search(source):
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Only the function's own statements, not those of a function nested in it.
                body = ast.get_source_segment(source, node) or ""
                nested = [ast.get_source_segment(source, n) or "" for n in ast.walk(node)
                          if n is not node
                          and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for inner in nested:
                    body = body.replace(inner, "")
                if not _ENDS_A_RUN.search(body):
                    continue
                calls = {getattr(c.func, "id", getattr(c.func, "attr", None))
                         for c in ast.walk(node) if isinstance(c, ast.Call)}
                found.append((f"{path.relative_to(ROOT)}:{node.name}", body,
                              "withdraw_with_run" in calls))
    return found


def test_every_place_that_ends_a_run_withdraws_its_decisions():
    found = _functions_that_end_a_run()
    # Not vacuous: cancel, remove, complete and undo are each found.
    names = {where.rsplit(":", 1)[1] for where, _, _ in found}
    assert {"cancel_run", "delete_run", "complete_compensation", "_maybe_finish_run"} <= names, names
    assert len(found) >= 4, found
    missing = [where for where, _, calls in found if not calls]
    assert not missing, (
        "These end a workflow run without withdrawing the decisions it left unanswered — "
        "their gates stay in Approvals, answerable, for a run that is over. Call "
        f"run_decisions.withdraw_with_run: {missing}")


def test_a_withdrawn_decision_offers_nothing_and_needs_nobody():
    decision = {"id": "dec_x", "status": rd.WITHDRAWN, "default_action": "hold",
                "recommendation": {"action": "resume_workflow", "workflow_run_id": "wfr_x"}}
    assert da.allowed_actions(decision) == []
    assert da.attention(decision) == "resolved"
    assert rd.WITHDRAWN not in da.ATTENTION_CANDIDATE_STATUSES


def test_withdrawing_covers_every_status_that_can_still_be_answered():
    assert set(da.ATTENTION_CANDIDATE_STATUSES) <= set(rd.UNANSWERED)


def test_a_decision_step_before_it_recorded_its_run_is_still_traced_to_it():
    class NoStepRow:
        def execute(self, *_a, **_k):
            class R:
                def scalar(self):
                    return None
            return R()

    decision = {"id": "dec_y", "recommendation": {
        "action": "resume_workflow",
        "reason": "Workflow wfr_0123456789abcdef0123 is waiting at decision step look."}}
    assert rd.owning_run_id(NoStepRow(), decision) == "wfr_0123456789abcdef0123"
    # A decision the run only borrowed (a panel's PRD approval) is not the run's.
    assert rd.owning_run_id(NoStepRow(), {"id": "d", "recommendation": {
        "action": "review_prd"}}) is None
