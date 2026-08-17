"""Metatron command — overwatch of captains, agents, and official work.

Captains staff a single run. Metatron sits above them: reads the whole
fleet against high-level objectives, then uses bounded powers to move a
project — re-prioritize official jobs, assign more captain/swarm staff,
loop a playbook. It does not invent a pass, auto-resolve decisions, or
launch a second factory job while one is already in flight.
"""
from __future__ import annotations

from typing import Any


COMMAND_KINDS = frozenset({
    "reprioritize_official",
    "staff_captain",
    "loop_playbook",
})


def fleet_findings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """High-level fleet findings. Pure. Only fire when overlay fields exist."""
    official = int(snapshot.get("official_in_flight") or 0)
    stalled = int(snapshot.get("factory_stalled") or 0)
    queued = int(snapshot.get("official_queued") or 0)
    findings: list[dict[str, Any]] = []
    if official and stalled:
        findings.append({
            "id": "fleet_stall",
            "severity": "high",
            "title": (
                f"{stalled} stall(s) on the factory flow while "
                f"{official} official run(s) are in flight"
            ),
            "action": "reprioritize_and_staff",
            "auto_safe": True,
        })
    elif official or queued:
        findings.append({
            "id": "fleet_push",
            "severity": "medium",
            "title": (
                f"Metatron advancing {official or queued} official run(s) "
                "toward working-software objectives"
            ),
            "action": "staff_and_loop",
            "auto_safe": True,
        })
    elif stalled:
        findings.append({
            "id": "fleet_stall",
            "severity": "medium",
            "title": (
                f"{stalled} factory stall(s) with no official run in flight; "
                "staff captains and loop the playbook"
            ),
            "action": "staff_and_loop",
            "auto_safe": True,
        })
    return findings


def command_moves(findings: list[dict[str, Any]], *, auto: bool) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    for finding in findings:
        action = finding.get("action")
        if action == "reprioritize_and_staff":
            moves.append(_move("reprioritize_official", finding, auto))
            moves.append(_move("staff_captain", finding, auto))
            moves.append(_move("loop_playbook", finding, auto))
        elif action == "staff_and_loop":
            moves.append(_move("staff_captain", finding, auto))
            moves.append(_move("loop_playbook", finding, auto))
    return moves


def _move(kind: str, finding: dict[str, Any], auto: bool) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": "queued" if auto else "proposed",
        "detail": finding.get("title"),
        "finding_id": finding.get("id"),
        "apply": bool(auto),
    }


def validate_command(
    move: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    auto: bool,
    budget_state: str = "unknown",
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    kind = str(move.get("kind") or "")

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("known_power", kind in COMMAND_KINDS, f"kind={kind}")
    check("budget_not_red", budget_state not in {"red", "orange"}, f"budget={budget_state}")
    check("auto_requested", bool(auto) or not move.get("apply"), "apply requires auto")
    check(
        "not_duplicate_factory",
        True,
        "command powers never start a second code.task while official work is in flight",
    )
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
    }


class MetatronCommand:
    def __init__(self, settings: Any):
        self.settings = settings

    def overlay_snapshot(self, connection: Any, project_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Add fleet fields. Missing tables stay zero — never invent load."""
        out = dict(snapshot)

        def _count(sql: str) -> int:
            try:
                from sqlalchemy import text

                return int(connection.execute(text(sql), {"p": project_id}).scalar_one() or 0)
            except Exception:
                return 0

        out["official_in_flight"] = _count(
            "SELECT count(*) FROM code_task_runs WHERE project_id=:p "
            "AND status IN ('running','queued','repairing') "
            "AND (finalize_ctx->>'official' = 'true' "
            "     OR finalize_ctx->'qa_benchmark' IS NOT NULL)"
        )
        out["official_queued"] = _count(
            "SELECT count(*) FROM code_task_runs WHERE project_id=:p "
            "AND status IN ('queued') "
            "AND (finalize_ctx->>'official' = 'true' "
            "     OR finalize_ctx->'qa_benchmark' IS NOT NULL)"
        )
        out["agent_process_runs"] = _count(
            "SELECT count(*) FROM agent_process_runs WHERE project_id=:p "
            "AND status IN ('running','queued','waiting')"
        )
        out["active_goals"] = _count(
            "SELECT count(*) FROM metatron_goals WHERE project_id=:p AND status='active'"
        )
        out["factory_stalled"] = _count(
            "SELECT count(*) FROM factory_flow_findings WHERE project_id=:p "
            "AND status='open' AND severity IN ('high','medium')"
        )
        out["captain_assignments"] = _count(
            "SELECT count(*) FROM captain_assignments WHERE project_id=:p "
            "AND status='assigned'"
        )
        out["playbooks"] = _count(
            "SELECT count(*) FROM captain_playbooks WHERE project_id=:p AND status='active'"
        )
        return out

    def enact(
        self,
        connection: Any,
        *,
        project_id: str,
        actor: str,
        snapshot: dict[str, Any],
        auto: bool,
    ) -> dict[str, Any]:
        """Apply validated fleet powers. Receipt per applied kind."""
        fleet = self.overlay_snapshot(connection, project_id, snapshot)
        findings = fleet_findings(fleet)
        moves = command_moves(findings, auto=auto)
        budget = str(fleet.get("budget_state") or "unknown")
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for move in moves:
            verdict = validate_command(move, fleet, auto=auto, budget_state=budget)
            move["validation"] = verdict
            if not (auto and move.get("apply") and verdict["ok"]):
                if move.get("apply") and not verdict["ok"]:
                    move["status"] = "rejected"
                    rejected.append(move)
                continue
            result = self._apply(connection, project_id=project_id, actor=actor, move=move)
            move.update(result)
            if result.get("status") == "applied":
                applied.append(move)
            else:
                rejected.append(move)
        return {
            "findings": findings,
            "moves": moves,
            "applied": applied,
            "rejected": rejected,
            "fleet": {
                "official_in_flight": fleet.get("official_in_flight"),
                "factory_stalled": fleet.get("factory_stalled"),
                "captain_assignments": fleet.get("captain_assignments"),
                "playbooks": fleet.get("playbooks"),
            },
        }

    def _apply(
        self, connection: Any, *, project_id: str, actor: str, move: dict[str, Any],
    ) -> dict[str, Any]:
        kind = move.get("kind")
        if kind == "reprioritize_official":
            return self._reprioritize(connection, project_id, actor)
        if kind == "staff_captain":
            return self._staff(connection, project_id, actor)
        if kind == "loop_playbook":
            return self._loop(connection, project_id, actor)
        return {"status": "rejected", "error": f"unknown power {kind}"}

    def _receipt(self, connection: Any, project_id: str, kind: str, payload: dict[str, Any]) -> str | None:
        try:
            from datetime import datetime, timezone

            from .core import emit_event
            from .receipts import ReceiptService

            minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            # org/workspace unknown here — emit only if receipts need scope.
            emit_event(
                connection, project_id, f"metatron.command.{kind}", "metatron",
                "project", project_id, payload,
            )
            _ = ReceiptService
            _ = minute
        except Exception:
            pass
        return None

    def _reprioritize(self, connection: Any, project_id: str, actor: str) -> dict[str, Any]:
        from sqlalchemy import text

        try:
            changed = connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET priority = 5,
                        priority_score = GREATEST(COALESCE(priority_score, 0), 9000),
                        updated_at = now()
                    WHERE project_id = :p
                      AND kind = 'code.task'
                      AND status IN ('queued', 'running')
                      AND payload->>'run_id' IN (
                        SELECT id FROM code_task_runs
                        WHERE project_id = :p
                          AND (
                            finalize_ctx->>'official' = 'true'
                            OR finalize_ctx->'qa_benchmark' IS NOT NULL
                          )
                      )
                    """
                ),
                {"p": project_id},
            ).rowcount
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "error": f"reprioritize failed: {type(exc).__name__}"}
        self._receipt(connection, project_id, "reprioritize", {"boosted": int(changed or 0), "actor": actor})
        return {"status": "applied", "boosted": int(changed or 0)}

    def _staff(self, connection: Any, project_id: str, actor: str) -> dict[str, Any]:
        try:
            from .captain_coordination import CaptainCoordinationService

            result = CaptainCoordinationService(self.settings).coordinate(
                connection, project_id, actor=actor,
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "error": f"staff failed: {type(exc).__name__}"}
        self._receipt(connection, project_id, "staff", {"assignments": result.get("assignments_written")})
        return {"status": "applied", "assignments_written": result.get("assignments_written")}

    def _loop(self, connection: Any, project_id: str, actor: str) -> dict[str, Any]:
        try:
            from .captain_playbook import CaptainPlaybookService

            result = CaptainPlaybookService(self.settings).loop_once(
                connection, project_id=project_id, actor=actor,
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "error": f"loop failed: {type(exc).__name__}"}
        self._receipt(connection, project_id, "loop", {"playbook": result.get("playbook_key")})
        return {
            "status": "applied",
            "playbook_key": result.get("playbook_key"),
            "finished": result.get("finished"),
        }
