"""Skill-invocation framework (spec §C: runtime that enforces per-skill retry/cost/permission +
mock mode).

Every skill call goes through `invoke`, which runs the same governance gauntlet the SkillSpec
declares: caller-permission gate (allowed_callers), cost gate (reserve budget in the skill's
category before running), and retry policy (up to retry.max_attempts). Each invocation is recorded
to skill_invocations with its decision, attempts, and outcome.

Mock mode runs the *entire* gauntlet — permission + retry semantics — but with **no real side
effects**: no budget is reserved and no external executor runs (a deterministic echo is returned).
That is exactly what canaries need to exercise a skill path without spending or mutating anything.
Real mode is driven by the worker runtime, which passes an `executor` callable.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import emit_event, make_id, record_policy_eval
from .entitlements import EntitlementBrokerService

# Rough per-estimator budget reservation (USD micros). The point is to exercise the cost *gate*,
# not to be precise — the real cost is reconciled by the executor's receipts.
COST_ESTIMATES_USD_MICROS = {
    "fixed": 1_000,
    "token_usage_estimate": 10_000,
    "provider_plan": 50_000,
    "storage_usage_estimate": 5_000,
    "compute_usage_estimate": 20_000,
}
DEFAULT_COST_USD_MICROS = 1_000


class SkillPermissionError(RuntimeError):
    pass


class SkillRuntimeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.entitlements = EntitlementBrokerService(settings)
        self.budget = BudgetGovernor(settings)

    def invoke(
        self,
        connection: Connection,
        project_id: str,
        skill_id: str,
        caller: str,
        inputs: dict[str, Any],
        *,
        mock: bool = True,
        executor: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        actor = actor or caller
        skill = self.entitlements.get_skill(skill_id)  # raises SkillNotFoundError
        permissions = skill.get("permissions") or {}
        allowed_callers = permissions.get("allowed_callers") or []
        retry = skill.get("retry") or {}
        max_attempts = max(int(retry.get("max_attempts") or 1), 1)
        cost = skill.get("cost") or {}
        category = cost.get("category") or "skill"
        estimate = self._estimate_cost(cost)
        mode = "mock" if mock else "real"

        # 1) Permission gate — caller must be allowed to invoke this skill.
        if not self._caller_allowed(caller, allowed_callers):
            reason = f"caller '{caller}' not in allowed_callers {allowed_callers}"
            record_policy_eval(
                connection, project_id, "skill.invoke", "deny",
                action=skill_id, reason=reason, detail={"caller": caller, "mode": mode})
            return self._record(
                connection, project_id, skill_id, caller, mode,
                status="denied", permission_decision="deny", attempts=0,
                max_attempts=max_attempts, estimate=estimate, reservation_id=None,
                reason=reason, result={}, actor=actor)

        # 2) Cost gate — reserve budget before running (real mode only; mock has no side effects).
        reservation_id: Optional[str] = None
        if not mock:
            reservation = self.budget.reserve(
                connection, project_id, category=category, amount_usd_micros=estimate,
                reason=f"skill:{skill_id}", expires_in_minutes=10)
            if reservation.get("status") != "reserved":
                reason = f"budget denied: {reservation.get('status')}"
                record_policy_eval(
                    connection, project_id, "skill.invoke", "deny",
                    action=skill_id, reason=reason,
                    detail={"caller": caller, "category": category, "estimate": estimate})
                return self._record(
                    connection, project_id, skill_id, caller, mode,
                    status="denied", permission_decision="allow", attempts=0,
                    max_attempts=max_attempts, estimate=estimate, reservation_id=None,
                    reason=reason, result={}, actor=actor)
            reservation_id = reservation["reservation_id"]

        # 3) Execute with the skill's retry policy.
        attempts = 0
        status = "failed"
        result: dict[str, Any] = {}
        last_error: Optional[str] = None
        while attempts < max_attempts:
            attempts += 1
            try:
                if mock:
                    result = {"mock": True, "skill_id": skill_id, "echo": inputs}
                elif executor is None:
                    raise SkillPermissionError(
                        "real skill invocation requires a server-side executor")
                else:
                    result = executor(inputs)
                status = "succeeded"
                break
            except Exception as exc:  # noqa: BLE001 — retry per policy; capture for the record
                last_error = str(exc)

        # Release the reservation — the executor records actual cost via its own receipts.
        if reservation_id is not None:
            self.budget.release(connection, project_id, reservation_id, f"skill:{skill_id} done")

        record_policy_eval(
            connection, project_id, "skill.invoke",
            "allow" if status == "succeeded" else "deny",
            action=skill_id,
            reason=None if status == "succeeded" else (last_error or "execution failed"),
            detail={"caller": caller, "mode": mode, "attempts": attempts})
        return self._record(
            connection, project_id, skill_id, caller, mode,
            status=status, permission_decision="allow", attempts=attempts,
            max_attempts=max_attempts, estimate=estimate, reservation_id=reservation_id,
            reason=None if status == "succeeded" else last_error, result=result, actor=actor)

    def list_invocations(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, skill_id, caller, mode, status, permission_decision, attempts,
                       max_attempts, cost_estimate_usd_micros, reservation_id, reason,
                       result, created_at
                FROM skill_invocations
                WHERE project_id = :p
                ORDER BY created_at DESC
                LIMIT :l
                """
            ),
            {"p": project_id, "l": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def _estimate_cost(self, cost: dict[str, Any]) -> int:
        estimator = cost.get("estimator")
        return COST_ESTIMATES_USD_MICROS.get(estimator, DEFAULT_COST_USD_MICROS)

    def _caller_allowed(self, caller: str, allowed_callers: list[str]) -> bool:
        if "*" in allowed_callers:
            return True
        return caller in allowed_callers

    def _record(
        self,
        connection: Connection,
        project_id: str,
        skill_id: str,
        caller: str,
        mode: str,
        *,
        status: str,
        permission_decision: str,
        attempts: int,
        max_attempts: int,
        estimate: int,
        reservation_id: Optional[str],
        reason: Optional[str],
        result: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        invocation_id = make_id("skinv")
        connection.execute(
            text(
                """
                INSERT INTO skill_invocations (
                    id, project_id, skill_id, caller, mode, status, permission_decision,
                    attempts, max_attempts, cost_estimate_usd_micros, reservation_id, reason, result
                )
                VALUES (
                    :id, :p, :skill_id, :caller, :mode, :status, :permission_decision,
                    :attempts, :max_attempts, :estimate, :reservation_id, :reason,
                    CAST(:result AS JSONB)
                )
                """
            ),
            {
                "id": invocation_id, "p": project_id, "skill_id": skill_id, "caller": caller,
                "mode": mode, "status": status, "permission_decision": permission_decision,
                "attempts": attempts, "max_attempts": max_attempts, "estimate": estimate,
                "reservation_id": reservation_id, "reason": reason, "result": json.dumps(result),
            },
        )
        emit_event(
            connection, project_id, "skill.invoked", actor, "skill_invocation", invocation_id,
            {"skill_id": skill_id, "caller": caller, "mode": mode, "status": status,
             "permission_decision": permission_decision, "attempts": attempts})
        return {
            "id": invocation_id, "project_id": project_id, "skill_id": skill_id, "caller": caller,
            "mode": mode, "status": status, "permission_decision": permission_decision,
            "attempts": attempts, "max_attempts": max_attempts,
            "cost_estimate_usd_micros": estimate, "reservation_id": reservation_id,
            "reason": reason, "result": result,
        }
