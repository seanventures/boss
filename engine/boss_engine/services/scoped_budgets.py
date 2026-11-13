"""BOSSAPP-863: budgets at org, project, agent (role) and workflow scope, checked mid-stage.

The budget governor reads one project-scope policy, and a code task reserves once and checks the
cap at stage boundaries. So one expensive agent could not be capped on its own, and a long stage
could overrun the cap before anything looked.

A SCOPED POLICY (``scoped_budget_policies``) caps the money one scope spends in a period (a UTC day
or calendar month):

* ``org``      — every project of the org (scope_id = the org id)
* ``project``  — one project (scope_id = the project id)
* ``role``     — one agent role (scope_id = the role key), in one project or, with no project, in
                 every project of the org
* ``workflow`` — every run of one workflow definition (scope_id = workflow_definitions.id)

with a warn threshold (``warn_pct``: an event, once per period) and ``hard_stop`` (refuse and stop).

Spend is MONEY on ``llm_calls`` (included_spend.LLM_MONEY_SQL: a subscription or local call is
$0) — the per-call ledger, the only one that knows the role and the act (and through the act, the
workflow) a call was for. Caps on the project policy keep counting settled receipts as before.

TWO CHECKS:

* ``reserve_refusal`` — BudgetGovernor.reserve asks before it reserves: every matching hard-stop
  policy whose spend plus this request passes its cap refuses the reservation.
* ``after_call`` — llm_telemetry asks after it records a paid call: a matching hard-stop policy
  at or past its cap SIGNALS the run to stop (``signal_stop``); the run stops at its next turn
  (harness_failures.check_turn / raise_if_tripped call ``raise_if_stopped``), not at the next
  stage boundary.

No active policy: nothing here runs a query more than once a ``CACHE_SECONDS`` (the default path
is the behaviour every install had before).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id

SCOPES = ("org", "project", "role", "workflow")
PERIODS = ("day", "month")
CACHE_SECONDS = 30.0

_LOCK = threading.Lock()
_ANY: dict[str, Any] = {"at": 0.0, "value": None}
_STOPS: dict[str, str] = {}
_WARNED: set[tuple[str, str]] = set()


class ScopedBudgetError(ValueError):
    """A policy that cannot be written, said for a person."""


class BudgetStop(RuntimeError):
    """A hard-stop scoped budget was reached while the run was working: it stops at this turn."""


# ── policies ───────────────────────────────────────────────────────────────────────────────

def _org_of_project(connection: Any, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()


def _any_active(connection: Any) -> bool:
    now = time.monotonic()
    with _LOCK:
        if _ANY["value"] is not None and now - _ANY["at"] < CACHE_SECONDS:
            return bool(_ANY["value"])
    try:
        with connection.begin_nested():
            value = bool(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM scoped_budget_policies WHERE status = 'active')")).scalar())
    except Exception:  # noqa: BLE001 — a store without the table has no scoped policies
        value = False
    with _LOCK:
        _ANY.update(at=now, value=value)
    return value


def _forget_cache() -> None:
    with _LOCK:
        _ANY.update(at=0.0, value=None)


def set_policy(connection: Any, *, org_id: str, scope_type: str, scope_id: str,
               cap_usd_micros: int, period: str = "month", warn_pct: float = 0.8,
               hard_stop: bool = True, project_id: Optional[str] = None,
               actor: str) -> dict[str, Any]:
    """Write (or replace) the one active policy for this scope and period."""
    scope_type = str(scope_type or "").strip().lower()
    if scope_type not in SCOPES:
        raise ScopedBudgetError(f"scope is one of {', '.join(SCOPES)}")
    if period not in PERIODS:
        raise ScopedBudgetError("period is day or month")
    cap = int(cap_usd_micros or 0)
    if cap <= 0:
        raise ScopedBudgetError("a cap is more than $0")
    warn = float(warn_pct if warn_pct is not None else 0.8)
    if not 0 < warn <= 1:
        raise ScopedBudgetError("warn at a share of the cap between 0 and 1")
    scope_id = str(scope_id or "").strip()
    if scope_type == "org":
        scope_id = org_id
    elif scope_type == "project":
        if _org_of_project(connection, scope_id) != org_id:
            raise ScopedBudgetError("No such project in this org.")
    elif scope_type == "role":
        if not scope_id:
            raise ScopedBudgetError("Name the agent (its role key).")
        if project_id and _org_of_project(connection, project_id) != org_id:
            raise ScopedBudgetError("No such project in this org.")
        found = connection.execute(text(
            "SELECT 1 FROM roles r JOIN projects p ON p.id = r.project_id "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE r.role_key = :r AND w.org_id = :o "
            "AND (CAST(:p AS TEXT) IS NULL OR r.project_id = :p) LIMIT 1"),
            {"r": scope_id, "o": org_id, "p": project_id}).first()
        if not found:
            raise ScopedBudgetError(f"No agent {scope_id!r} here.")
    elif scope_type == "workflow":
        found = connection.execute(text(
            "SELECT d.project_id FROM workflow_definitions d WHERE d.id = :w"),
            {"w": scope_id}).scalar()
        if not found or _org_of_project(connection, str(found)) != org_id:
            raise ScopedBudgetError("No such workflow in this org.")
    connection.execute(text(
        "UPDATE scoped_budget_policies SET status = 'removed', updated_at = now() "
        "WHERE org_id = :o AND scope_type = :t AND scope_id = :s AND period = :per "
        "AND COALESCE(project_id, '') = COALESCE(CAST(:p AS TEXT), '') AND status = 'active'"),
        {"o": org_id, "t": scope_type, "s": scope_id, "per": period,
         "p": project_id if scope_type == "role" else None})
    policy_id = make_id("sbp")
    connection.execute(text(
        "INSERT INTO scoped_budget_policies (id, org_id, scope_type, scope_id, project_id, period, "
        " cap_usd_micros, warn_pct, hard_stop, created_by) VALUES (:id, :o, :t, :s, :p, :per, :cap, "
        " :warn, :hard, :by)"),
        {"id": policy_id, "o": org_id, "t": scope_type, "s": scope_id,
         "p": project_id if scope_type == "role" else None, "per": period, "cap": cap,
         "warn": warn, "hard": bool(hard_stop), "by": actor})
    _forget_cache()
    return get_policy(connection, policy_id)


def remove_policy(connection: Any, org_id: str, policy_id: str) -> bool:
    gone = connection.execute(text(
        "UPDATE scoped_budget_policies SET status = 'removed', updated_at = now() "
        "WHERE id = :i AND org_id = :o AND status = 'active'"), {"i": policy_id, "o": org_id}).rowcount
    _forget_cache()
    return bool(gone)


def get_policy(connection: Any, policy_id: str) -> dict[str, Any]:
    row = connection.execute(text("SELECT * FROM scoped_budget_policies WHERE id = :i"),
                             {"i": policy_id}).mappings().first()
    return _public(connection, dict(row)) if row else {}


def list_policies(connection: Any, org_id: str, *, with_spend: bool = True) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM scoped_budget_policies WHERE org_id = :o AND status = 'active' "
        "ORDER BY scope_type, scope_id, period"), {"o": org_id}).mappings().all()
    return [_public(connection, dict(r), with_spend=with_spend) for r in rows]


def _public(connection: Any, row: dict[str, Any], *, with_spend: bool = True) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("id", "org_id", "scope_type", "scope_id", "project_id", "period",
                                   "cap_usd_micros", "hard_stop", "status", "created_by")}
    out["cap_usd_micros"] = int(out["cap_usd_micros"] or 0)
    out["warn_pct"] = float(row.get("warn_pct") or 0)
    for k in ("created_at", "updated_at"):
        out[k] = row[k].isoformat() if isinstance(row.get(k), datetime) else None
    if with_spend:
        out["spent_usd_micros"] = spent(connection, row)
    return out


# ── spend ──────────────────────────────────────────────────────────────────────────────────

def spent(connection: Any, policy: dict[str, Any], now: Optional[datetime] = None) -> int:
    """MONEY this policy's scope has spent in its current period (llm_calls)."""
    from .budget import budget_period
    from .included_spend import LLM_MONEY_SQL

    bounds = budget_period(str(policy.get("period") or "month"), now)
    params = {"start": bounds["start"], "end": bounds["end"], "s": policy["scope_id"],
              "o": policy["org_id"], "p": policy.get("project_id")}
    in_org = ("l.project_id IN (SELECT p.id FROM projects p JOIN workspaces w ON "
              "w.id = p.workspace_id WHERE w.org_id = :o)")
    where = {
        "org": in_org,
        "project": "l.project_id = :s",
        "role": ("l.role = :s AND " + ("l.project_id = :p" if policy.get("project_id") else in_org)),
        "workflow": ("l.act_id IN (SELECT a.act_id FROM agent_acts a JOIN workflow_runs r ON "
                     "r.id = a.workflow_run_id WHERE r.workflow_definition_id = :s)"),
    }[str(policy["scope_type"])]
    money = LLM_MONEY_SQL.replace("included_in_subscription", "l.included_in_subscription") \
        .replace("cost_usd_micros", "l.cost_usd_micros")
    return int(connection.execute(text(
        f"SELECT COALESCE(SUM({money}), 0) FROM llm_calls l WHERE {where} "
        "AND l.created_at >= :start AND l.created_at < :end"), params).scalar() or 0)


def _workflow_of(connection: Any, workflow_run_id: Optional[str]) -> Optional[str]:
    if not workflow_run_id:
        return None
    return connection.execute(text(
        "SELECT workflow_definition_id FROM workflow_runs WHERE id = :r"),
        {"r": workflow_run_id}).scalar()


def matching(connection: Any, project_id: str, *, role: Optional[str] = None,
             workflow_run_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every active policy that covers a call in `project_id` by `role` in `workflow_run_id`."""
    org_id = _org_of_project(connection, project_id)
    if not org_id:
        return []
    workflow = _workflow_of(connection, workflow_run_id)
    rows = connection.execute(text(
        "SELECT * FROM scoped_budget_policies WHERE org_id = :o AND status = 'active' AND ("
        " scope_type = 'org'"
        " OR (scope_type = 'project' AND scope_id = :p)"
        " OR (scope_type = 'role' AND scope_id = :r AND (project_id IS NULL OR project_id = :p))"
        " OR (scope_type = 'workflow' AND scope_id = :w))"),
        {"o": org_id, "p": project_id, "r": str(role or ""), "w": str(workflow or "")}
    ).mappings().all()
    return [dict(r) for r in rows]


def _who() -> tuple[Optional[str], Optional[str]]:
    from .llm_telemetry import _llm_ctx

    ctx = dict(_llm_ctx.get() or {})
    return (ctx.get("speaker_role") or ctx.get("role_key") or None,
            ctx.get("workflow_run_id") or None)


def _label(policy: dict[str, Any]) -> str:
    noun = {"org": "the org", "project": "the project", "role": f"agent {policy['scope_id']}",
            "workflow": f"workflow {policy['scope_id']}"}[str(policy["scope_type"])]
    return f"{noun}'s {policy.get('period') or 'month'}ly budget"


def reserve_refusal(connection: Any, project_id: str, amount_usd_micros: int, *,
                    role: Optional[str] = None, workflow_run_id: Optional[str] = None
                    ) -> Optional[dict[str, Any]]:
    """The refusal BudgetGovernor.reserve returns when a hard-stop policy would be passed, else
    None. Role and workflow default to the model-call context the reservation is made in."""
    if not _any_active(connection):
        return None
    ctx_role, ctx_wf = _who()
    role, workflow_run_id = role or ctx_role, workflow_run_id or ctx_wf
    for policy in matching(connection, project_id, role=role, workflow_run_id=workflow_run_id):
        if not policy["hard_stop"]:
            continue
        used = spent(connection, policy)
        cap = int(policy["cap_usd_micros"])
        if used + max(0, int(amount_usd_micros or 0)) > cap:
            from .budget import budget_period

            bounds = budget_period(str(policy["period"]))
            return {"status": "denied", "refusal": "scoped_cap_exceeded", "transient": False,
                    "decision_id": None, "policy_id": policy["id"],
                    "scope_type": policy["scope_type"], "scope_id": policy["scope_id"],
                    "cap_usd_micros": cap, "spent_usd_micros": used,
                    "resets_at": bounds["end"].isoformat() if hasattr(bounds["end"], "isoformat") else None,
                    "message": (f"Refused: {_label(policy)} is spent "
                                f"(${used / 1e6:,.2f} of ${cap / 1e6:,.2f}). Raise it in Govern › "
                                "Limits, or wait for the period to reset.")}
    return None


# ── mid-stage ──────────────────────────────────────────────────────────────────────────────

def signal_stop(run_id: str, sentence: str) -> None:
    with _LOCK:
        _STOPS.setdefault(str(run_id), sentence)
        while len(_STOPS) > 500:
            _STOPS.pop(next(iter(_STOPS)))


def stop_for(run_id: Optional[str]) -> Optional[str]:
    if not run_id:
        return None
    with _LOCK:
        return _STOPS.get(str(run_id))


def clear(run_id: str) -> None:
    with _LOCK:
        _STOPS.pop(str(run_id), None)


def raise_if_stopped(run_id: Optional[str] = None) -> None:
    """Called at every turn of a governed run: a run told to stop stops here."""
    if not _STOPS:
        return
    if run_id is None:
        from .run_secret_redaction import current_run_id

        run_id = current_run_id()
    sentence = stop_for(run_id)
    if sentence:
        raise BudgetStop(sentence)


def after_call(connection: Any, *, project_id: Optional[str], role: Optional[str],
               workflow_run_id: Optional[str], run_id: Optional[str]) -> list[str]:
    """After a PAID call is recorded: warn once per period at warn_pct, and signal the run to
    stop at its next turn when a hard-stop policy is at or past its cap. Returns the ids of the
    policies that stopped it. Never raises."""
    try:
        if not project_id or not _any_active(connection):
            return []
        stopped: list[str] = []
        from .budget import budget_period
        from .core import emit_event

        for policy in matching(connection, str(project_id), role=role,
                               workflow_run_id=workflow_run_id):
            used = spent(connection, policy)
            cap = int(policy["cap_usd_micros"])
            period_key = str(budget_period(str(policy["period"]))["start"])
            if used >= cap and policy["hard_stop"]:
                sentence = (f"Stopped mid-stage: {_label(policy)} is spent "
                            f"(${used / 1e6:,.2f} of ${cap / 1e6:,.2f}). Raise it in Govern › Limits.")
                if run_id:
                    signal_stop(str(run_id), sentence)
                stopped.append(str(policy["id"]))
                key = (str(policy["id"]), "stop:" + period_key)
            elif used >= float(policy["warn_pct"] or 0.8) * cap:
                key = (str(policy["id"]), "warn:" + period_key)
            else:
                continue
            with _LOCK:
                if key in _WARNED:
                    continue
                _WARNED.add(key)
            emit_event(connection, str(project_id),
                       "budget.scoped_stopped" if key[1].startswith("stop") else "budget.scoped_warning",
                       "budget_governor", "scoped_budget_policy", str(policy["id"]),
                       {"scope_type": policy["scope_type"], "scope_id": policy["scope_id"],
                        "spent_usd_micros": used, "cap_usd_micros": cap, "run_id": run_id})
        return stopped
    except Exception:  # noqa: BLE001 — a budget check never fails the call that paid
        return []


def reset_for_tests() -> None:
    with _LOCK:
        _STOPS.clear()
        _WARNED.clear()
        _ANY.update(at=0.0, value=None)
