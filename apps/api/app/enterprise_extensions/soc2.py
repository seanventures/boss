"""SOC2 evidence collection (spec §F enterprise). Aggregates the control evidence BOSS already
produces — durable audit events, the policy-eval audit trail, break-glass grants + postmortems,
governed change decisions, and the canary/eval proof suites — into a single SOC2 evidence bundle
artifact mapped to the Trust Services Common Criteria. Auditor-ready, generated from live data.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from boss_engine.config import Settings
from boss_engine.services.core import create_artifact, emit_event, now_utc


def _scalar(connection: Connection, sql: str, project_id: str) -> int:
    return int(connection.execute(text(sql), {"p": project_id}).scalar() or 0)


class SOC2EvidenceService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def collect_evidence(
        self, connection: Connection, project_id: str, *, actor: str = "compliance"
    ) -> dict[str, Any]:
        # CC4/CC7 — monitoring & audit logging
        events = _scalar(connection, "SELECT count(*) FROM events WHERE project_id = :p", project_id)
        policy_evals = _scalar(
            connection, "SELECT count(*) FROM policy_eval_log WHERE project_id = :p", project_id)
        policy_denies = _scalar(
            connection,
            "SELECT count(*) FROM policy_eval_log WHERE project_id = :p AND decision = 'deny'",
            project_id)

        # CC6 — logical access (break-glass with mandatory postmortems)
        bg_total = _scalar(
            connection, "SELECT count(*) FROM break_glass_grants WHERE project_id = :p", project_id)
        bg_with_pm = _scalar(
            connection,
            "SELECT count(*) FROM break_glass_grants WHERE project_id = :p "
            "AND postmortem_task_id IS NOT NULL",
            project_id)

        # CC8 — change management (governed decisions + self-improvement rollouts + quality gates)
        decisions_total = _scalar(
            connection, "SELECT count(*) FROM decisions WHERE project_id = :p", project_id)
        decisions_resolved = _scalar(
            connection,
            "SELECT count(*) FROM decisions WHERE project_id = :p AND status <> 'pending'",
            project_id)
        gates_total = _scalar(
            connection, "SELECT count(*) FROM quality_gates WHERE project_id = :p", project_id)
        gates_passed = _scalar(
            connection,
            "SELECT count(*) FROM quality_gates WHERE project_id = :p AND status = 'pass'",
            project_id)

        # CC7 — proof suites (canary + eval harness)
        canary = connection.execute(
            text("SELECT status, created_at FROM canary_runs WHERE project_id = :p "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id},
        ).mappings().first()
        evals = connection.execute(
            text("SELECT status, created_at FROM eval_runs WHERE project_id = :p "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id},
        ).mappings().first()

        controls = [
            {
                "id": "CC4.1",
                "name": "Monitoring & durable audit logging",
                "status": "satisfied" if events > 0 else "attention",
                "evidence": {"audit_events": events, "policy_evaluations": policy_evals},
            },
            {
                "id": "CC6.1",
                "name": "Logical access — break-glass with mandatory postmortem",
                "status": "satisfied" if bg_total == bg_with_pm else "attention",
                "evidence": {"break_glass_grants": bg_total, "with_postmortem": bg_with_pm},
            },
            {
                "id": "CC6.8",
                "name": "Policy enforcement audit trail",
                "status": "satisfied" if policy_evals > 0 else "attention",
                "evidence": {"policy_evaluations": policy_evals, "denied": policy_denies},
            },
            {
                "id": "CC7.2",
                "name": "Continuous verification (canary + eval suites)",
                "status": "satisfied" if (canary and canary["status"] == "pass") else "attention",
                "evidence": {
                    "latest_canary": canary["status"] if canary else None,
                    "latest_eval": evals["status"] if evals else None,
                },
            },
            {
                "id": "CC8.1",
                "name": "Change management — governed decisions & quality gates",
                "status": "satisfied" if decisions_total > 0 else "attention",
                "evidence": {
                    "decisions_total": decisions_total,
                    "decisions_resolved": decisions_resolved,
                    "quality_gates_total": gates_total,
                    "quality_gates_passed": gates_passed,
                },
            },
        ]
        satisfied = sum(1 for control in controls if control["status"] == "satisfied")
        bundle = {
            "project_id": project_id,
            "generated_at": now_utc().isoformat(),
            "generated_by": actor,
            "framework": "SOC2 (Trust Services Common Criteria)",
            "controls": controls,
            "summary": {
                "controls_total": len(controls),
                "controls_satisfied": satisfied,
                "controls_attention": len(controls) - satisfied,
            },
        }
        artifact_id = create_artifact(
            connection, self.settings, project_id, None, "soc2_evidence_bundle",
            f"SOC2 evidence bundle ({satisfied}/{len(controls)} controls satisfied)", bundle)
        emit_event(
            connection, project_id, "soc2.evidence_collected", actor,
            "artifact", artifact_id,
            {"controls_satisfied": satisfied, "controls_total": len(controls)})
        return {"artifact_id": artifact_id, **bundle}
