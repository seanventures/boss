"""Engineering captain as coordinator — pick agents, assign work, convene panels.

The PRD process used a fixed 11-step cast. The captain now assesses a roster
(skills + capabilities), decides who is needed *now*, assigns tasks, convenes
a review panel when the work warrants it, and refuses "finished" until the
factory actually completed with honest verify.

Hard rules:
  * Decisions / deadletters never auto-apply.
  * Missing evidence is not a pass.
  * Captain does not silently edit BOSS.
  * Spendy factory work stays on an existing official run or a Receipted job.
"""
from __future__ import annotations

import json
from typing import Any, Optional


# Capability catalog. Fitness is evidence + need tags, not a self-score.
ROSTER: list[dict[str, Any]] = [
    {
        "id": "engineering_captain",
        "name": "Engineering captain",
        "kind": "captain",
        "skills": ["coordinate", "assign", "verify", "accept"],
        "capabilities": ["staffing", "panel", "finish_gate"],
        "serves": ["coordinate", "build", "verify", "accept"],
    },
    {
        "id": "product_manager",
        "name": "Product manager",
        "kind": "role",
        "skills": ["prd", "intake", "improve"],
        "capabilities": ["accept_prd", "improve_prd"],
        "serves": ["prd_intake", "prd_improve"],
    },
    {
        "id": "chief_architect",
        "name": "Chief architect",
        "kind": "role",
        "skills": ["architecture", "review"],
        "capabilities": ["review_prd", "certify_spec"],
        "serves": ["prd_review", "certify"],
    },
    {
        "id": "cto",
        "name": "CTO",
        "kind": "role",
        "skills": ["feasibility", "review"],
        "capabilities": ["review_prd", "certify_spec"],
        "serves": ["prd_review", "certify"],
    },
    {
        "id": "vp_product",
        "name": "VP product",
        "kind": "role",
        "skills": ["product", "review"],
        "capabilities": ["review_prd", "certify_spec"],
        "serves": ["prd_review", "certify"],
    },
    {
        "id": "qa",
        "name": "QA",
        "kind": "role",
        "skills": ["verify", "regression"],
        "capabilities": ["qa_review", "certify_spec"],
        "serves": ["verify", "certify"],
    },
    {
        "id": "ux_engineer",
        "name": "UI/UX engineer",
        "kind": "role",
        "skills": ["walkthrough", "a11y", "jtbd"],
        "capabilities": ["ux_review", "certify_spec"],
        "serves": ["ux", "certify"],
    },
    {
        "id": "factory_driver",
        "name": "Factory driver",
        "kind": "swarm",
        "skills": ["implement", "repair"],
        "capabilities": ["launch_factory_job"],
        "serves": ["build", "repair"],
    },
    {
        "id": "reviewer",
        "name": "Reviewer swarm",
        "kind": "swarm",
        "skills": ["code_review"],
        "capabilities": ["propose_review"],
        "serves": ["review"],
    },
    {
        "id": "queue_helper",
        "name": "Queue helper",
        "kind": "swarm",
        "skills": ["unstick"],
        "capabilities": ["launch_factory_job"],
        "serves": ["unstick"],
    },
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS captain_assignments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT,
  process_run_id TEXT,
  agent_id TEXT NOT NULL,
  role TEXT NOT NULL,
  task TEXT NOT NULL,
  reason TEXT NOT NULL,
  timing TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'assigned',
  panel JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_captain_assignments_project
  ON captain_assignments(project_id, run_id, updated_at DESC);
"""


def roster() -> list[dict[str, Any]]:
    return [dict(item) for item in ROSTER]


def assess_agent(agent: dict[str, Any], needs: list[str]) -> dict[str, Any]:
    """Fit is overlap of needs vs serves. Missing skill stays unfit, never 100."""
    serves = list(agent.get("serves") or [])
    matched = [need for need in needs if need in serves]
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "kind": agent.get("kind"),
        "skills": list(agent.get("skills") or []),
        "capabilities": list(agent.get("capabilities") or []),
        "matched_needs": matched,
        "fit": bool(matched),
    }


def needs_from_snapshot(snap: dict[str, Any]) -> list[str]:
    """What the captain needs *now*. Evidence only."""
    status = str(snap.get("status") or "")
    phase = str(snap.get("phase") or "")
    klass = str((snap.get("diagnosis") or {}).get("klass") or "")
    official = bool(snap.get("official"))
    tests = snap.get("tests_passed")
    has_process = bool(snap.get("process_run_id"))
    needs: list[str] = ["coordinate"]
    if official and not has_process and status in {"running", "queued", ""}:
        needs.extend(["prd_intake", "prd_review", "prd_improve", "build"])
    if klass in {"factory_crash", "factory_error", "verify_dishonest"}:
        needs.extend(["repair", "verify", "review"])
    if klass == "queued_serial":
        needs.append("unstick")
    if status == "running" and phase in {"implement", "run_tests", "write_tests"}:
        needs.append("build")
    if status == "running" and phase in {"review_panel", "check_tests"}:
        needs.append("review")
    if tests is True or phase in {"review_panel", "check_tests", "done"}:
        needs.extend(["verify", "ux", "certify"])
    if official:
        needs.extend(["verify", "accept"])
        if "ux" not in needs:
            needs.append("ux")
    # Unique, stable order
    seen: list[str] = []
    for need in needs:
        if need not in seen:
            seen.append(need)
    return seen


def convene_panel(snap: dict[str, Any], needs: list[str]) -> Optional[dict[str, Any]]:
    """Captain convenes a panel only when the work warrants review."""
    klass = str((snap.get("diagnosis") or {}).get("klass") or "")
    tests = snap.get("tests_passed")
    official = bool(snap.get("official"))
    phase = str(snap.get("phase") or "")
    if klass in {"factory_crash", "verify_dishonest"}:
        return {
            "roles": ["chief_architect", "qa", "engineering_captain"],
            "size": 3,
            "reason": f"Failure class {klass} needs a repair panel.",
            "when": "now",
        }
    if "certify" in needs or tests is True or phase in {"review_panel", "check_tests"}:
        roles = ["chief_architect", "cto", "qa"]
        if official or "ux" in needs:
            roles.append("ux_engineer")
        return {
            "roles": roles,
            "size": 5 if official else len(roles),
            "reason": "Work reached verification; captain convenes a certify panel.",
            "when": "after_verify",
        }
    if "prd_review" in needs:
        return {
            "roles": ["chief_architect", "cto", "vp_product"],
            "size": 3,
            "reason": "New official work needs PRD review before build.",
            "when": "before_build",
        }
    return None


def finish_check(snap: dict[str, Any]) -> dict[str, Any]:
    """Captain will not accept invented completion."""
    status = str(snap.get("status") or "")
    tests = snap.get("tests_passed")
    klass = str((snap.get("diagnosis") or {}).get("klass") or "")
    blockers: list[str] = []
    if status != "completed":
        blockers.append(f"factory status is {status or 'unknown'}, not completed")
    if tests is not True:
        blockers.append("tests_passed is not true")
    if klass in {"factory_crash", "verify_dishonest", "factory_error"}:
        blockers.append(f"open failure class {klass}")
    if snap.get("error"):
        blockers.append("run still has an error")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "verdict": "finished" if not blockers else "not_finished",
    }


def plan_staffing(snap: dict[str, Any]) -> dict[str, Any]:
    """Captain staffing plan: who, why, when. Unused agents stay idle."""
    needs = needs_from_snapshot(snap)
    assessed = [assess_agent(agent, needs) for agent in ROSTER]
    chosen = [item for item in assessed if item["fit"]]
    panel = convene_panel(snap, needs)
    finish = finish_check(snap)
    assignments: list[dict[str, Any]] = []
    phase = str(snap.get("phase") or "")
    for agent in chosen:
        timing = "later"
        task = "standby"
        if "coordinate" in agent["matched_needs"]:
            timing, task = "now", "coordinate_and_finish"
        elif "build" in agent["matched_needs"] and phase in {"", "queued", "implement", "run_tests", "write_tests"}:
            timing, task = "now", "build_or_repair"
        elif "repair" in agent["matched_needs"]:
            timing, task = "now", "repair_failure"
        elif "verify" in agent["matched_needs"] and (
            snap.get("tests_passed") is True or phase in {"run_tests", "review_panel", "check_tests", "done"}
        ):
            timing, task = "now", "verify_honestly"
        elif "prd_review" in agent["matched_needs"] and not snap.get("process_run_id"):
            timing, task = "now", "review_prd"
        elif "prd_intake" in agent["matched_needs"] and not snap.get("process_run_id"):
            timing, task = "now", "accept_prd"
        elif "certify" in agent["matched_needs"] and panel and panel.get("when") in {"now", "after_verify"}:
            timing, task = panel["when"], "sit_on_panel"
        elif "ux" in agent["matched_needs"]:
            timing, task = "after_verify", "ux_review"
        elif "unstick" in agent["matched_needs"]:
            timing, task = "now", "hold_queue"
        assignments.append({
            "agent_id": agent["id"],
            "name": agent["name"],
            "kind": agent["kind"],
            "task": task,
            "reason": "matched " + ", ".join(agent["matched_needs"]),
            "timing": timing,
            "skills": agent["skills"],
        })
    return {
        "run_id": snap.get("id"),
        "needs": needs,
        "roster": assessed,
        "assignments": assignments,
        "panel": panel,
        "finish": finish,
        "note": (
            "Captain assigned only agents whose capabilities match current needs. "
            "Unused roster members stay idle. Finish is evidence-only."
        ),
    }


class CaptainCoordinationService:
    def __init__(self, settings: Any):
        self.settings = settings
        self._schema_ready = False

    def ensure_schema(self, connection: Any) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def coordinate(
        self, connection: Any, project_id: str, *, actor: str = "engineering_captain",
    ) -> dict[str, Any]:
        """Staff official/in-flight work, persist assignments, convene panels."""
        from .factory_flow import FactoryFlowService

        self.ensure_schema(connection)
        board = FactoryFlowService(self.settings).board(connection, project_id)
        plans: list[dict[str, Any]] = []
        assigned = 0
        for item in board.get("items") or []:
            plan = plan_staffing(item)
            plans.append(plan)
            assigned += self._persist_assignments(
                connection, project_id, item, plan, actor=actor,
            )
        # ONLY WHEN IT STAFFED SOMEONE. Every sweep re-plans the same board, and 2,130 of the
        # 2,182 captain.coordinated events on 2026-10-13 said "20 plans, 0 assignments".
        if assigned:
            emit = __import__(
                "boss_engine.services.core", fromlist=["emit_event"]
            ).emit_event
            emit(
                connection, project_id, "captain.coordinated", actor, "project", project_id,
                {"plans": len(plans), "assignments": assigned},
            )
        return {
            "project_id": project_id,
            "plans": plans,
            "assignments_written": assigned,
            "in_flight": board.get("in_flight"),
            "stalled": board.get("stalled"),
        }

    def plan_for_run(
        self, connection: Any, project_id: str, run_id: str,
    ) -> dict[str, Any]:
        from .factory_flow import FactoryFlowService

        flow = FactoryFlowService(self.settings)
        row = None
        try:
            from sqlalchemy import text

            row = connection.execute(
                text(
                    "SELECT id, status, phase, title, error, state, finalize_ctx, updated_at "
                    "FROM code_task_runs WHERE id=:id AND project_id=:p"
                ),
                {"id": run_id, "p": project_id},
            ).mappings().first()
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            return plan_staffing({"id": run_id, "status": "", "phase": ""})
        snap = flow.snapshot(connection, project_id, dict(row))
        return plan_staffing(snap)

    def _persist_assignments(
        self,
        connection: Any,
        project_id: str,
        item: dict[str, Any],
        plan: dict[str, Any],
        *,
        actor: str,
    ) -> int:
        from sqlalchemy import text

        from .core import make_id

        written = 0
        panel = plan.get("panel") or {}
        for assignment in plan.get("assignments") or []:
            if assignment.get("timing") != "now":
                continue
            dedupe = (
                f"{item.get('id')}:{assignment.get('agent_id')}:{assignment.get('task')}"
            )
            existing = connection.execute(
                text(
                    "SELECT id FROM captain_assignments "
                    "WHERE project_id=:p AND run_id=:r AND agent_id=:a AND task=:t "
                    "AND status='assigned'"
                ),
                {
                    "p": project_id,
                    "r": item.get("id"),
                    "a": assignment.get("agent_id"),
                    "t": assignment.get("task"),
                },
            ).scalar_one_or_none()
            if existing:
                continue
            connection.execute(
                text(
                    """
                    INSERT INTO captain_assignments
                      (id, project_id, run_id, process_run_id, agent_id, role, task,
                       reason, timing, status, panel)
                    VALUES
                      (:id, :p, :rid, :pr, :agent, :role, :task, :reason, :timing,
                       'assigned', CAST(:panel AS JSONB))
                    """
                ),
                {
                    "id": make_id("casg"),
                    "p": project_id,
                    "rid": item.get("id"),
                    "pr": item.get("process_run_id"),
                    "agent": assignment.get("agent_id"),
                    "role": assignment.get("kind") or "role",
                    "task": assignment.get("task"),
                    "reason": (assignment.get("reason") or "")[:400],
                    "timing": assignment.get("timing"),
                    "panel": json.dumps(panel),
                },
            )
            written += 1
            _ = dedupe
            _ = actor
        return written
