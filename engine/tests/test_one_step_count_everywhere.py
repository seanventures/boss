"""One step count for a workflow everywhere (stranger test r2, 2026-11-17).

The plan said "13 steps", the strip numbered around hands (1 2 4 5 …), and the running task said
"step 2/15" for the same Standard Product Delivery: the run counted the fork/join markers. Every
count now leaves markers out through command_center.counted_steps / is_marker_step.
"""
from __future__ import annotations

from boss_engine.services import command_center, workflow_catalogue
from boss_engine.services.project_experience import add_project


def _spec():
    steps = [{"id": "plan", "kind": "capability"}, {"id": "fork", "kind": "parallel"},
             {"id": "a", "kind": "capability"}, {"id": "b", "kind": "capability"},
             {"id": "join", "kind": "join"}, {"id": "pr", "kind": "capability"}]
    return {s["id"]: s for s in steps}


def test_a_run_counts_no_markers():
    spec = _spec()
    rows = [{"step_id": "plan", "status": "succeeded"}, {"step_id": "fork", "status": "succeeded"},
            {"step_id": "a", "status": "running"}]
    assert command_center.counted_steps(spec, rows) == (1, 4)
    done, count = command_center.counted_steps(spec, rows)
    assert command_center.step_index(done, count, "running") == 2


def test_catalogue_count_leaves_markers_out():
    spd = next(e for e in workflow_catalogue.catalogue_summary(prd_auto_approve=False)
               if e["key"] == "standard_product_delivery")
    markers = sum(1 for s in spd["steps"] if s["capability"] in command_center.MARKER_KINDS)
    assert markers > 0  # it has a fork and a join
    assert spd["step_count"] == len(spd["steps"]) - markers


def test_add_project_uses_the_same_rule():
    import inspect
    assert "is_marker_step" in inspect.getsource(add_project.workflow_steps)
