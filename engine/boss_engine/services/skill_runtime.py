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
from .skill_executors import SkillExecutorContext, resolve_executor

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
        executor_context: Optional[SkillExecutorContext] = None,
        skill_spec: Optional[dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        actor = actor or caller
        created_by_job_id = (
            executor_context.created_by_job_id
            if not mock and executor_context is not None
            else None
        )
        if created_by_job_id:
            existing = self._invocation_for_job(
                connection,
                project_id,
                skill_id,
                caller,
                created_by_job_id,
            )
            if existing is not None:
                return existing
        skill = skill_spec or self.entitlements.get_skill(
            skill_id, project_id, connection=connection
        )
        if str(skill.get("id") or "") != skill_id:
            raise ValueError("skill_spec id does not match skill_id")
        self.entitlements.validate_skill_spec(skill, "runtime")
        permissions = skill.get("permissions") or {}
        allowed_callers = permissions.get("allowed_callers") or []
        retry = skill.get("retry") or {}
        max_attempts = max(int(retry.get("max_attempts") or 1), 1)
        cost = skill.get("cost") or {}
        category = cost.get("category") or "skill"
        estimate = self._estimate_cost(cost)
        mode = "mock" if mock else "real"

        # 1) Permission gate — caller must be allowed to invoke this skill.
        #
        # GIVING A SKILL TO AN AGENT IS THE GRANT, and this is where it is cashed in. Skills
        # declare the callers that existed when they were written, so an operator who assigns
        # one to an agent outside that list would otherwise get a saved assignment that is
        # denied the first time it runs — a feature that looks like it works and does not.
        #
        # The same argument `workflow_custom_steps` makes for the runner applies here, and the
        # grant has the same four properties: EXPLICIT (somebody chose this agent for this
        # skill, by name, in this project), SCOPED (only to skills a `role_skills` row exists
        # for), RECORDED (`skill.assigned_to_agent` names who granted it), and REVOCABLE
        # (unassign and the grant goes with it).
        #
        # What is widened is WHO may call. Everything else is untouched: the risk floor, the
        # approval gate, the budget reservation and the effect receipt all still apply, which
        # is why a tier-3 merge still needs its CEO and CTO approvals no matter who holds it.
        granted_by_assignment = False
        if not self._caller_allowed(caller, allowed_callers):
            granted_by_assignment = self._assigned_to_agent(
                connection, project_id, caller, skill_id)
        if not self._caller_allowed(caller, allowed_callers) and not granted_by_assignment:
            reason = f"caller '{caller}' not in allowed_callers {allowed_callers}"
            record_policy_eval(
                connection, project_id, "skill.invoke", "deny",
                action=skill_id, reason=reason, detail={"caller": caller, "mode": mode})
            return self._record(
                connection, project_id, skill_id, caller, mode,
                status="denied", permission_decision="deny", attempts=0,
                max_attempts=max_attempts, estimate=estimate, reservation_id=None,
                actual_cost=None, cost_receipt_id=None,
                created_by_job_id=created_by_job_id, reason=reason, result={}, actor=actor)

        if granted_by_assignment:
            # SAY WHY IT WAS ALLOWED. Without this the trail shows a caller the spec does not
            # list, invoking successfully, with nothing to explain it — which reads as the
            # permission gate having failed rather than as a grant somebody made deliberately.
            record_policy_eval(
                connection, project_id, "skill.invoke", "allow",
                action=skill_id,
                reason=(f"caller '{caller}' is not in allowed_callers {allowed_callers}, but "
                        f"this project has assigned {skill_id} to it"),
                detail={"caller": caller, "mode": mode, "grant": "role_assignment"})

        if not mock and executor is None:
            executor = resolve_executor(skill_id, context=executor_context)
            if executor is None:
                reason = f"No server-side executor is registered for skill '{skill_id}'."
                record_policy_eval(
                    connection, project_id, "skill.invoke", "deny",
                    action=skill_id, reason=reason,
                    detail={"caller": caller, "mode": mode})
                return self._record(
                    connection, project_id, skill_id, caller, mode,
                    status="failed", permission_decision="allow", attempts=0,
                    max_attempts=max_attempts, estimate=estimate, reservation_id=None,
                    actual_cost=None, cost_receipt_id=None,
                    created_by_job_id=created_by_job_id, reason=reason, result={}, actor=actor)

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
                    actual_cost=None, cost_receipt_id=None,
                    created_by_job_id=created_by_job_id, reason=reason, result={}, actor=actor)
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
                else:
                    assert executor is not None
                    candidate = executor(inputs)
                    if not isinstance(candidate, dict):
                        raise TypeError("skill executor must return an object")
                    if candidate.get("status") in {"blocked", "failed", "in_progress"}:
                        raise RuntimeError(
                            "skill executor returned non-terminal success status: "
                            f"{candidate['status']}"
                        )
                    result = candidate
                status = "succeeded"
                break
            except Exception as exc:  # noqa: BLE001 — retry per policy; capture for the record
                last_error = str(exc)

        actual_cost: Optional[int] = None
        cost_receipt_id: Optional[str] = None
        # Receipt-backed executors can settle the reservation with their actual cost.
        # Internal/no-cost executors release it instead of fabricating a provider receipt.
        if reservation_id is not None:
            receipt_id = result.get("receipt_id") if status == "succeeded" else None
            reported_actual = (
                result.get("actual_cost_usd_micros")
                if status == "succeeded"
                else None
            )
            if (
                receipt_id
                and isinstance(reported_actual, int)
                and not isinstance(reported_actual, bool)
                and reported_actual >= 0
            ):
                try:
                    with connection.begin_nested():
                        settlement = self.budget.consume(
                            connection,
                            project_id,
                            reservation_id,
                            str(receipt_id),
                            category,
                            skill_id,
                            estimate,
                            reported_actual,
                            actor,
                        )
                    actual_cost = reported_actual
                    cost_receipt_id = settlement["cost_receipt"]["id"]
                except Exception as exc:  # noqa: BLE001 — preserve effect evidence
                    last_error = f"cost reconciliation failed: {exc}"
                    result = {**result, "cost_reconciliation_error": str(exc)}
                    status = "failed"
                    self.budget.release(
                        connection,
                        project_id,
                        reservation_id,
                        f"skill:{skill_id} reconciliation failed",
                    )
            else:
                self.budget.release(
                    connection, project_id, reservation_id, f"skill:{skill_id} done")

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
            actual_cost=actual_cost, cost_receipt_id=cost_receipt_id,
            created_by_job_id=created_by_job_id,
            reason=None if status == "succeeded" else last_error, result=result, actor=actor)

    def list_invocations(
        self, connection: Connection, project_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, created_by_job_id, skill_id, caller, mode, status,
                       permission_decision, attempts,
                       max_attempts, cost_estimate_usd_micros,
                       actual_cost_usd_micros, cost_receipt_id, reservation_id,
                       reason, result, created_at
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

    def _assigned_to_agent(
        self, connection: Connection, project_id: str, caller: str, skill_id: str
    ) -> bool:
        """Does this project hold an explicit assignment of this skill to this agent?

        Read at the moment of the call, not cached: an operator who takes a skill back off an
        agent expects the next run to be denied, not the run after the process restarts.
        """
        if not caller:
            return False
        try:
            return connection.execute(
                text(
                    "SELECT 1 FROM role_skills WHERE project_id = :p AND role_key = :r "
                    "AND skill_id = :s LIMIT 1"
                ),
                {"p": project_id, "r": caller, "s": skill_id},
            ).first() is not None
        except Exception:  # noqa: BLE001
            # The table not existing yet must DENY, not allow. A permission gate that opens
            # when its own lookup fails is not a gate.
            return False

    def _invocation_for_job(
        self,
        connection: Connection,
        project_id: str,
        skill_id: str,
        caller: str,
        created_by_job_id: str,
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, created_by_job_id, skill_id, caller, mode,
                       status, permission_decision, attempts, max_attempts,
                       cost_estimate_usd_micros, actual_cost_usd_micros,
                       cost_receipt_id, reservation_id, reason, result
                FROM skill_invocations
                WHERE created_by_job_id = :created_by_job_id
                  AND project_id = :project_id
                  AND skill_id = :skill_id
                  AND caller = :caller
                  AND mode = 'real'
                """
            ),
            {
                "created_by_job_id": created_by_job_id,
                "project_id": project_id,
                "skill_id": skill_id,
                "caller": caller,
            },
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

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
        actual_cost: Optional[int],
        cost_receipt_id: Optional[str],
        created_by_job_id: Optional[str],
        reason: Optional[str],
        result: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        invocation_id = make_id("skinv")
        connection.execute(
            text(
                """
                INSERT INTO skill_invocations (
                    id, project_id, created_by_job_id, skill_id, caller, mode,
                    status, permission_decision,
                    attempts, max_attempts, cost_estimate_usd_micros,
                    actual_cost_usd_micros, cost_receipt_id, reservation_id, reason, result
                )
                VALUES (
                    :id, :p, :created_by_job_id, :skill_id, :caller, :mode,
                    :status, :permission_decision,
                    :attempts, :max_attempts, :estimate, :actual_cost,
                    :cost_receipt_id, :reservation_id, :reason, CAST(:result AS JSONB)
                )
                """
            ),
            {
                "id": invocation_id, "p": project_id, "skill_id": skill_id, "caller": caller,
                "created_by_job_id": created_by_job_id,
                "mode": mode, "status": status, "permission_decision": permission_decision,
                "attempts": attempts, "max_attempts": max_attempts, "estimate": estimate,
                "actual_cost": actual_cost, "cost_receipt_id": cost_receipt_id,
                "reservation_id": reservation_id, "reason": reason,
                "result": json.dumps(result),
            },
        )
        emit_event(
            connection, project_id, "skill.invoked", actor, "skill_invocation", invocation_id,
            {"skill_id": skill_id, "caller": caller, "mode": mode, "status": status,
             "permission_decision": permission_decision, "attempts": attempts})
        return {
            "id": invocation_id, "project_id": project_id, "skill_id": skill_id, "caller": caller,
            "created_by_job_id": created_by_job_id,
            "mode": mode, "status": status, "permission_decision": permission_decision,
            "attempts": attempts, "max_attempts": max_attempts,
            "cost_estimate_usd_micros": estimate,
            "actual_cost_usd_micros": actual_cost,
            "cost_receipt_id": cost_receipt_id,
            "reservation_id": reservation_id,
            "reason": reason, "result": result,
        }
