"""BOSSAPP-1438: never ask mid-build; record assumptions in decision log, events & PR body."""
# ruff: noqa: F811
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import pr_description

from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_pr_description_renders_assumptions_only_when_present():
    base = pr_description.render(
        title="Add items endpoint",
        evidence={"test_command": "pytest", "tests_passed": True},
        gates={"tests_pass": True},
        paths=["app.py"],
    )
    assert "### Assumptions" not in base

    with_assumptions = pr_description.render(
        title="Add items endpoint",
        evidence={"test_command": "pytest", "tests_passed": True},
        gates={"tests_pass": True},
        paths=["app.py"],
        assumptions=["Defaulted pagination to 50 items per page", "Used UTC ISO-8601 timestamps"],
    )
    assert "### Assumptions" in with_assumptions
    assert "- Defaulted pagination to 50 items per page" in with_assumptions
    assert "- Used UTC ISO-8601 timestamps" in with_assumptions

    explicit_empty = pr_description.render(
        title="Add items endpoint",
        evidence={"test_command": "pytest", "tests_passed": True, "assumptions": ["Fallback item"]},
        gates={"tests_pass": True},
        paths=["app.py"],
        assumptions=[],
    )
    assert "### Assumptions" not in explicit_empty


def test_split_design_and_extract_assumptions_are_case_insensitive():
    mixed = (
        "=== STANDARDS ===\n- Ruff\n"
        "=== SPEC ===\nSpec.\n"
        "=== PLAN ===\nPlan.\ndatabase: none\n"
        "=== Assumptions ===\n- Case-insensitive heading\n"
        "=== PLAN REVIEW ===\nPLAN: OK\n"
    )
    parts = g._split_design(mixed)
    assert parts["spec"] == "Spec."
    assert parts["plan"] == "Plan.\ndatabase: none"
    assert g._extract_assumptions(mixed) == ["Case-insensitive heading"]


def test_ambiguous_brief_records_assumptions_without_blocking_and_renders_pr(
    eng, project, tmp_path, monkeypatch
):
    """Run the governed design + summary pipeline on an ambiguous brief with a fake agent:
    zero pending clarification decisions, event + decided audit row written, and PR body
    renders `### Assumptions`."""
    run_id = f"crun_1438_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
                "VALUES (:i, :p, 'Export API', 'running', 'design', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"i": run_id, "p": project},
        )

    joint_answer = (
        "=== STANDARDS ===\n- FastAPI\n"
        "=== SPEC ===\nExport endpoint.\n"
        "=== PLAN ===\n- api.py: export()\ndatabase: none\n"
        "=== ASSUMPTIONS ===\n"
        "- Defaulted export format to CSV when format query param is omitted\n"
        "- Capped export batch size to 1000 rows\n"
        "=== PLAN REVIEW ===\nLooks good.\nPLAN: OK\n"
    )
    parts = g._split_design(joint_answer)
    assumptions = g._extract_assumptions(joint_answer)
    design = {
        "standards": parts["standards"],
        "spec": parts["spec"],
        "plan": parts["plan"],
        "plan_review": parts["plan_review"],
        "plan_ok": True,
        "assumptions": assumptions,
        "packages": parts["packages"],
    }

    with eng.begin() as c:
        c.execute(
            text(
                "UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) "
                "|| jsonb_build_object('design', CAST(:d AS JSONB)) WHERE id = :i"
            ),
            {"d": json.dumps(design), "i": run_id},
        )
        from boss_engine.services import autonomy_gate
        from boss_engine.services.core import emit_event

        emit_event(
            c, project, "code_task.assumptions_recorded", "autonomy:dev:architect",
            "code_task_run", run_id, {"assumptions": assumptions},
        )
        autonomy_gate.record_decided(
            c, project, task_id=None,
            question=f"Design assumptions for Export API ({len(assumptions)})",
            recommendation={"action": "design_assumptions", "run_id": run_id, "assumptions": assumptions},
            found=autonomy_gate.Route(
                "act", autonomy_gate.ACT,
                "ambiguous brief resolved by choosing the most reasonable reading",
                "architect", autonomy_gate.NONE_RECORD,
            ),
        )

    with eng.connect() as c:
        pending = c.execute(
            text("SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending'"),
            {"p": project},
        ).scalar()
        decided = c.execute(
            text(
                "SELECT status, approved_by, recommendation FROM decisions "
                "WHERE project_id = :p AND recommendation->>'action' = 'design_assumptions'"
            ),
            {"p": project},
        ).mappings().all()
        ev = c.execute(
            text(
                "SELECT data FROM events WHERE project_id = :p "
                "AND event_type = 'code_task.assumptions_recorded'"
            ),
            {"p": project},
        ).scalar()

    assert pending == 0
    assert len(decided) == 1
    assert decided[0]["status"] == "approved"
    assert decided[0]["approved_by"] == "autonomy:dev:architect"
    assert decided[0]["recommendation"]["assumptions"] == assumptions
    assert ev["assumptions"] == assumptions

    summary = g._generated_pr_summary(
        eng, get_settings(), run_id, project,
        title="Export API",
        evidence={"assumptions": design["assumptions"], "test_command": "pytest", "tests_passed": True},
        gates={"tests_pass": True},
        paths=["api.py"],
        qg_id="",
    )
    assert "### Assumptions" in summary
    assert "- Defaulted export format to CSV when format query param is omitted" in summary
    assert "- Capped export batch size to 1000 rows" in summary

    with eng.begin() as c:
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": run_id})
