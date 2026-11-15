"""DEPLOYMENTS: an agent spec version live in one of a project's environments (AP-P3,
BOSSAPP-1054; docs/design/AGENT_PLATFORM.md §4.4 "Deployments and environments", D6).

A deployment is (agent_key, environment) → version, per project. Environments are dev, staging
and prod (D6: per project; the HOST is an attribute of the deployment, not an environment).

  deploy      put a version in an environment (dev / staging take it at once)
  promote     move one environment's version to the next: the A3 checks run first (the spec is
              valid, the latest dataset experiment of that version if there is one, the cost per
              run against the live version), and PROD needs a person's approval through a
              Decision (`promote_agent_deployment`); approving it applies it
  rollback    re-point an environment to the version it had before (or a named one)

A RUN IS PINNED TO THE VERSION IT STARTED ON. Admission writes the version onto the run
(agent_runs.agent_version + agent_version_hash and finalize_ctx.agent_version), and the
deployed-agent runner compiles THAT version on every drive — a resume after a person answered
an interrupt included. So promoting v8 while v7 runs leaves the in-flight v7 runs on v7, and the
promotion screen says how many ("3 runs in flight stay on v7 until they finish").

The roles projection (P1's `active` version: the grant checks read it) is moved by publishing,
not by deploying; a deployed run is offered exactly its agent's grants either way.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

ENVIRONMENTS = ("dev", "staging", "prod")
#: Promotion INTO these needs a person's approval through a Decision.
APPROVAL_ENVIRONMENTS = frozenset({"prod"})
#: The Decision a production promotion waits on.
ACTION = "promote_agent_deployment"
#: The run statuses that hold a version "in flight" (thread_runs.ACTIVE plus `pending`).
IN_FLIGHT = ("pending", "queued", "running", "awaiting_approval", "drained", "budget_wait")
#: Runners a deployment may name (agent_run_worker.RUNNERS); the legacy ones are bots.migrated.
RUNNERS = ("deployed_agent", "focused_agent", "wakeup", "squad_owner", "priors_build")


class DeploymentError(ValueError):
    """Something the operator can fix, phrased for them. `status` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _out(row: Any) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for key in ("created_at", "updated_at", "promoted_at"):
        out[key] = _iso(out.get(key))
    out["expose"] = _json(out.get("expose"), {})
    out["metadata"] = _json(out.get("metadata"), {})
    # P8a placement (boss-agent-hosts-lld.md §3.1): JSON-safe whatever the driver returns.
    out["resources"] = _json(out.get("resources"), {})
    out["placement"] = _json(out.get("placement"), {})
    if out.get("vip") is not None:
        out["vip"] = str(out["vip"])
    if out.get("vip_ports") is not None:
        out["vip_ports"] = [int(p) for p in out["vip_ports"]]
    return out


def _env(environment: str) -> str:
    env = str(environment or "").strip().lower()
    if env not in ENVIRONMENTS:
        raise DeploymentError(f"environment must be one of {', '.join(ENVIRONMENTS)}; "
                              f"got {environment!r}.")
    return env


# ── Reading ────────────────────────────────────────────────────────────────────────────────────

def get(connection: Connection, project_id: str, agent_key: str, environment: str) -> Optional[dict]:
    return _out(connection.execute(text(
        "SELECT * FROM agent_deployments WHERE project_id = :p AND agent_key = :a "
        "AND environment = :e"), {"p": project_id, "a": agent_key, "e": _env(environment)}
    ).mappings().first())


def by_id(connection: Connection, deployment_id: str) -> Optional[dict]:
    return _out(connection.execute(text("SELECT * FROM agent_deployments WHERE id = :d"),
                                   {"d": deployment_id}).mappings().first())


def for_agent(connection: Connection, project_id: str, agent_key: str) -> list[dict]:
    rows = connection.execute(text(
        "SELECT * FROM agent_deployments WHERE project_id = :p AND agent_key = :a "
        "AND status = 'live'"), {"p": project_id, "a": agent_key}).mappings().all()
    order = {e: i for i, e in enumerate(ENVIRONMENTS)}
    return sorted((_out(r) for r in rows), key=lambda d: order.get(d["environment"], 9))


def live_for_run(connection: Connection, project_id: str, agent_key: str,
                 environment: str = "prod") -> dict:
    """The deployment a new run of `agent_key` in `environment` starts on; refuses when the
    agent is not deployed there."""
    found = get(connection, project_id, agent_key, environment)
    if found is None or found["status"] != "live":
        raise DeploymentError(f"{agent_key} is not deployed to {environment} in this project.",
                              status=404)
    if found["runner"] == "deployed_agent" and not found.get("version"):
        raise DeploymentError(f"{agent_key} has no version live in {environment}.", status=409)
    return found


def _version_row(connection: Connection, project_id: str, agent_key: str,
                 version: int) -> dict:
    from ..agents import versions

    try:
        return versions.get_version(connection, project_id, agent_key, int(version))
    except versions.VersionError as exc:
        raise DeploymentError(str(exc), status=exc.status) from exc


def in_flight(connection: Connection, project_id: str, agent_key: str, environment: str,
              version: Optional[int] = None) -> int:
    """Runs of this agent in this environment that have not ended (on `version` when given)."""
    return int(connection.execute(text(
        "SELECT count(*) FROM agent_runs WHERE project_id = :p AND agent_key = :a "
        "AND environment = :e AND status = ANY(:s) "
        "AND (CAST(:v AS INTEGER) IS NULL OR agent_version = CAST(:v AS INTEGER))"),
        {"p": project_id, "a": agent_key, "e": environment, "s": list(IN_FLIGHT),
         "v": version}).scalar() or 0)


def _history(connection: Connection, *, project_id: str, agent_key: str, environment: str,
             action: str, actor: str, from_version: Optional[int], to_version: Optional[int],
             status: str = "applied", checks: Optional[dict] = None,
             decision_id: Optional[str] = None) -> str:
    hid = make_id("adh")
    # P8a: each history row records the placement the version runs with (runtime, vm_mode).
    connection.execute(text("""
        INSERT INTO agent_deployment_history (id, project_id, agent_key, environment, action,
               status, from_version, to_version, checks, decision_id, actor, applied_at,
               runtime, vm_mode)
        SELECT :id, :p, :a, :e, :act, :s, :f, :t, CAST(:c AS JSONB), :d, :by,
               CASE WHEN :s = 'applied' THEN now() END,
               COALESCE((SELECT runtime FROM agent_deployments WHERE project_id = :p
                          AND agent_key = :a AND environment = :e), 'local'),
               (SELECT vm_mode FROM agent_deployments WHERE project_id = :p
                 AND agent_key = :a AND environment = :e)"""),
        {"id": hid, "p": project_id, "a": agent_key, "e": environment, "act": action,
         "s": status, "f": from_version, "t": to_version, "c": json.dumps(checks or {}, default=str),
         "d": decision_id, "by": actor})
    return hid


def history(connection: Connection, project_id: str, agent_key: str, *, limit: int = 20) -> list:
    rows = connection.execute(text(
        "SELECT id, environment, action, status, from_version, to_version, checks, decision_id, "
        "actor, created_at, applied_at FROM agent_deployment_history WHERE project_id = :p "
        "AND agent_key = :a ORDER BY created_at DESC, id DESC LIMIT :n"),
        {"p": project_id, "a": agent_key, "n": int(limit)}).mappings().all()
    return [{**dict(r), "created_at": _iso(r["created_at"]), "applied_at": _iso(r["applied_at"]),
             "checks": _json(r["checks"], {})} for r in rows]


# ── Writing ────────────────────────────────────────────────────────────────────────────────────

def deploy(connection: Connection, project_id: str, agent_key: str, environment: str, *,
           actor: str, version: Optional[int] = None, host_id: Optional[str] = None,
           host_label: Optional[str] = None, runner: str = "deployed_agent",
           source: str = "operator", display_name: Optional[str] = None,
           action: str = "deploy", expose: Optional[dict] = None,
           double_text: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
    """Put `version` live in `environment` (upsert), recording what it replaced."""
    env = _env(environment)
    if runner not in RUNNERS:
        raise DeploymentError(f"runner must be one of {', '.join(RUNNERS)}.")
    content_hash = None
    if runner == "deployed_agent":
        if version is None:
            raise DeploymentError("say which version to deploy.")
        row = _version_row(connection, project_id, agent_key, int(version))
        content_hash = row["content_hash"]
        if row["kind"] != "agent":
            raise DeploymentError(f"{agent_key} v{version} is a profile; deploy the agent it "
                                  "extends.")
        named = ((_json(row.get("spec"), {}) or {}).get("metadata") or {}).get("name")
        display_name = display_name or (str(named) if named else None)
    current = get(connection, project_id, agent_key, env)
    name = display_name or (current or {}).get("display_name") or _display_name(
        connection, project_id, agent_key)
    if current is None:
        did = make_id("adep")
        connection.execute(text("""
            INSERT INTO agent_deployments (id, project_id, agent_key, display_name, environment,
                   version, content_hash, runner, source, host_id, host_label, expose,
                   double_text, metadata, promoted_by, promoted_at, created_by)
            VALUES (:id, :p, :a, :n, :e, :v, :h, :r, :src, :host, :hl,
                    CAST(:x AS JSONB), :dt, CAST(:m AS JSONB), :by, now(), :by)"""),
            {"id": did, "p": project_id, "a": agent_key, "n": name, "e": env, "v": version,
             "h": content_hash, "r": runner, "src": source, "host": host_id, "hl": host_label,
             "x": json.dumps(expose or {"rest": True, "mcp": True, "a2a": False}),
             "dt": double_text or "enqueue", "m": json.dumps(metadata or {}), "by": actor})
    else:
        did = current["id"]
        connection.execute(text("""
            UPDATE agent_deployments SET version = :v, content_hash = :h,
                   previous_version = CASE WHEN version IS DISTINCT FROM :v THEN version
                                           ELSE previous_version END,
                   runner = :r, display_name = :n, status = 'live',
                   host_id = COALESCE(:host, host_id), host_label = COALESCE(:hl, host_label),
                   expose = COALESCE(CAST(:x AS JSONB), expose),
                   double_text = COALESCE(:dt, double_text),
                   metadata = metadata || CAST(:m AS JSONB),
                   promoted_by = :by, promoted_at = now(), updated_at = now()
             WHERE id = :id"""),
            {"id": did, "v": version, "h": content_hash, "r": runner, "n": name,
             "host": host_id, "hl": host_label, "x": json.dumps(expose) if expose else None,
             "dt": double_text, "m": json.dumps(metadata or {}), "by": actor})
    _history(connection, project_id=project_id, agent_key=agent_key, environment=env,
             action=action, actor=actor, from_version=(current or {}).get("version"),
             to_version=version)
    emit_event(connection, project_id, f"agent.deployment.{action}", actor, "agent_deployment",
               did, {"agent_key": agent_key, "environment": env, "version": version,
                     "from_version": (current or {}).get("version"), "runner": runner})
    return by_id(connection, did) or {}


def _display_name(connection: Connection, project_id: str, agent_key: str) -> str:
    name = connection.execute(text(
        "SELECT display_name FROM roles WHERE project_id = :p AND role_key = :k"),
        {"p": project_id, "k": agent_key}).scalar()
    return str(name or agent_key.replace("_", " ").capitalize())


def set_host(connection: Connection, project_id: str, agent_key: str, environment: str, *,
             actor: str, host_id: Optional[str], host_label: Optional[str]) -> dict:
    current = get(connection, project_id, agent_key, environment)
    if current is None:
        raise DeploymentError(f"{agent_key} is not deployed to {environment}.", status=404)
    connection.execute(text(
        "UPDATE agent_deployments SET host_id = :h, host_label = :l, updated_at = now() "
        "WHERE id = :id"), {"h": host_id, "l": host_label, "id": current["id"]})
    _history(connection, project_id=project_id, agent_key=agent_key, environment=current[
        "environment"], action="host", actor=actor, from_version=current.get("version"),
        to_version=current.get("version"), checks={"host_id": host_id, "host_label": host_label})
    return by_id(connection, current["id"]) or {}


def rollback(connection: Connection, project_id: str, agent_key: str, environment: str, *,
             actor: str, to_version: Optional[int] = None) -> dict:
    """Re-point `environment` to the version it had before (or `to_version`). Runs in flight
    stay on the version they started on."""
    current = get(connection, project_id, agent_key, environment)
    if current is None:
        raise DeploymentError(f"{agent_key} is not deployed to {environment}.", status=404)
    target = to_version if to_version is not None else current.get("previous_version")
    if target is None:
        raise DeploymentError(f"{agent_key} in {environment} has no earlier version to roll "
                              "back to.", status=409)
    if int(target) == int(current.get("version") or 0):
        raise DeploymentError(f"{environment} already runs v{target}.", status=409)
    return deploy(connection, project_id, agent_key, environment, actor=actor,
                  version=int(target), runner=current["runner"], action="rollback")


def admit_run(connection: Connection, deployment: dict, *, thread_id: str, messages: list,
              trigger: str, actor: str = "boss", policy: Optional[str] = None,
              extra: Optional[dict] = None, run_id: Optional[str] = None,
              parent_run_id: Optional[str] = None, priority: Optional[int] = None) -> dict:
    """Admit a run of this deployment on `thread_id` (thread_runs.admit: the run and its job in
    one transaction), PINNED to the version the deployment runs now: the version is written on
    the run row and in its finalize_ctx, and the runner compiles that version on every drive."""
    from . import thread_runs

    fctx = {"runner": deployment["runner"], "deployment_id": deployment["id"],
            "agent_key": deployment["agent_key"], "agent_version": deployment.get("version"),
            "environment": deployment["environment"], "started_by": actor, **(extra or {})}
    if (deployment.get("runtime") or "local") == "microvm":
        # P8a: the run executes on one of the deployment's replicas, through the run plane;
        # every job it ever gets carries this (jobs.enqueue_job), so no local worker claims it.
        fctx["runtime"] = "microvm"
    admitted = thread_runs.admit(
        connection, project_id=deployment["project_id"], thread_id=thread_id,
        messages=messages, finalize_ctx=fctx, policy=policy, trigger=trigger,
        agent_version_hash=deployment.get("content_hash"), run_id=run_id,
        parent_run_id=parent_run_id, priority=priority)
    connection.execute(text("""
        UPDATE agent_runs SET deployment_id = :d, trigger_id = :t, agent_key = :a,
               agent_version = :v, environment = :e WHERE id = :r"""),
        {"d": deployment["id"], "t": (extra or {}).get("trigger_id"),
         "a": deployment["agent_key"], "v": deployment.get("version"),
         "e": deployment["environment"], "r": admitted["run_id"]})
    return admitted


# ── Promotion and its checks (wireframe A3) ────────────────────────────────────────────────────

def cost_per_run(connection: Connection, project_id: str, agent_key: str,
                 version: Optional[int], *, days: int = 30) -> Optional[dict]:
    """The mean model cost of this version's runs that spent anything (llm_calls by run), over
    `days`; None when it has no such runs."""
    if version is None:
        return None
    row = connection.execute(text("""
        SELECT count(*) AS runs, COALESCE(avg(spent), 0) AS mean FROM (
          SELECT r.id, sum(l.cost_usd_micros) AS spent FROM agent_runs r
            JOIN llm_calls l ON l.run_id = r.id
           WHERE r.project_id = :p AND r.agent_key = :a AND r.agent_version = :v
             AND r.created_at >= :since
           GROUP BY r.id) s"""),
        {"p": project_id, "a": agent_key, "v": int(version),
         "since": datetime.now(timezone.utc) - timedelta(days=days)}).mappings().first()
    if not row or not int(row["runs"] or 0):
        return None
    return {"runs": int(row["runs"]), "usd_micros": int(row["mean"] or 0)}


def _eval_result(connection: Connection, project_id: str, agent_key: str,
                 version: int) -> Optional[dict]:
    """The latest finished dataset experiment of this version (AP-P4), if any."""
    exp = connection.execute(text(
        "SELECT id, name, dataset_id, finished_at FROM dataset_experiments WHERE project_id = :p "
        "AND agent_key = :a AND spec_version = :v AND status = 'completed' "
        "ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1"),
        {"p": project_id, "a": agent_key, "v": int(version)}).mappings().first()
    if exp is None:
        return None
    counts = connection.execute(text(
        "SELECT count(*) AS total, count(*) FILTER (WHERE passed) AS passed "
        "FROM dataset_experiment_results WHERE experiment_id = :e"), {"e": exp["id"]}
    ).mappings().first()
    total, passed = int(counts["total"] or 0), int(counts["passed"] or 0)
    return {"experiment_id": exp["id"], "name": exp["name"], "passed": passed, "total": total,
            "ok": total > 0 and passed == total, "finished_at": _iso(exp["finished_at"])}


def promotion_checks(connection: Connection, project_id: str, agent_key: str, *,
                     from_environment: str, to_environment: str,
                     version: Optional[int] = None) -> dict:
    """What the A3 promotion card shows before anyone presses Promote:

      spec       the version validates as boss.agent/v1 and names only governance the kernel has
      eval       the latest dataset experiment of the version, when there is one (P4)
      cost       its mean cost per run against the target's live version (a delta, when both
                 have runs)
      approval   whether a person must approve it (prod) and who may
      in_flight  how many runs of the target's live version are running: they stay on it
    """
    from ..agents import spec as S
    from ..kernel.agent_spec import SpecGovernanceError, check_governance

    src_env, dst_env = _env(from_environment), _env(to_environment)
    source = get(connection, project_id, agent_key, src_env)
    target = get(connection, project_id, agent_key, dst_env)
    candidate = version if version is not None else (source or {}).get("version")
    if candidate is None:
        raise DeploymentError(f"{agent_key} has no version in {src_env} to promote.", status=409)
    row = _version_row(connection, project_id, agent_key, int(candidate))
    errors = S.validate(_json(row["spec"], {}))
    spec_ok, spec_detail = not errors, ""
    if spec_ok:
        try:
            check_governance(S.compile_spec(S.load_spec(_json(row["spec"], {}))))
        except (S.SpecError, SpecGovernanceError) as exc:
            spec_ok, spec_detail = False, str(exc)[:300]
    else:
        spec_detail = "; ".join(f"{e.get('path')}: {e.get('message')}" for e in errors[:3])
    live_version = (target or {}).get("version")
    evaluation = _eval_result(connection, project_id, agent_key, int(candidate))
    cand_cost = cost_per_run(connection, project_id, agent_key, int(candidate))
    live_cost = cost_per_run(connection, project_id, agent_key, live_version)
    delta = None
    if cand_cost and live_cost and live_cost["usd_micros"]:
        delta = round((cand_cost["usd_micros"] - live_cost["usd_micros"])
                      / live_cost["usd_micros"] * 100, 1)
    needs_approval = dst_env in APPROVAL_ENVIRONMENTS
    flight = in_flight(connection, project_id, agent_key, dst_env, live_version) \
        if live_version is not None else 0
    checks = [
        {"key": "spec", "ok": spec_ok, "label": "Spec valid" if spec_ok else "Spec invalid",
         "detail": spec_detail},
        {"key": "eval", "ok": None if evaluation is None else evaluation["ok"],
         "label": (f"Eval set {evaluation['passed']}/{evaluation['total']}" if evaluation
                   else "No eval set run on this version"),
         "detail": evaluation or {}},
        {"key": "cost", "ok": None if delta is None else delta <= 25,
         "label": (f"Cost per run {'+' if delta > 0 else ''}{delta:g}%" if delta is not None
                   else "Cost per run: not enough runs to compare"),
         "detail": {"candidate": cand_cost, "live": live_cost, "delta_pct": delta}},
    ]
    blocking = [c["key"] for c in checks if c["ok"] is False and c["key"] in ("spec", "eval")]
    return {
        "agent_key": agent_key, "from_environment": src_env, "to_environment": dst_env,
        "version": int(candidate), "live_version": live_version,
        "checks": checks, "can_promote": not blocking, "blocked_by": blocking,
        "approval": {"required": needs_approval,
                     "who": "a person who may approve decisions in this project"
                     if needs_approval else None},
        "in_flight": {"runs": flight, "version": live_version,
                      "note": (f"{flight} run{'s' if flight != 1 else ''} in flight stay on "
                               f"v{live_version} until they finish. New runs use v{candidate}.")
                      if live_version is not None else f"New runs use v{candidate}."},
    }


def promote(connection: Connection, project_id: str, agent_key: str, *, from_environment: str,
            to_environment: str, actor: str, version: Optional[int] = None) -> dict:
    """Promote `from_environment`'s version (or `version`) into `to_environment`. A blocking
    check refuses it; prod opens a Decision and applies on approval; others apply now."""
    checks = promotion_checks(connection, project_id, agent_key,
                              from_environment=from_environment, to_environment=to_environment,
                              version=version)
    dst = checks["to_environment"]
    if not checks["can_promote"]:
        _history(connection, project_id=project_id, agent_key=agent_key, environment=dst,
                 action="promote", actor=actor, from_version=checks["live_version"],
                 to_version=checks["version"], status="refused", checks=checks)
        raise DeploymentError(f"v{checks['version']} cannot be promoted: "
                              f"{', '.join(checks['blocked_by'])} check failed.", status=409)
    source = get(connection, project_id, agent_key, checks["from_environment"])
    runner = (source or {}).get("runner") or "deployed_agent"
    if not checks["approval"]["required"]:
        out = deploy(connection, project_id, agent_key, dst, actor=actor,
                     version=checks["version"], runner=runner, action="promote")
        out = _carry_runtime(connection, project_id, agent_key, checks["from_environment"], dst,
                             actor=actor) or out
        return {"status": "applied", "deployment": out, "checks": checks}
    pending = connection.execute(text(
        "SELECT decision_id FROM agent_deployment_history WHERE project_id = :p AND agent_key = :a "
        "AND environment = :e AND action = 'promote' AND status = 'awaiting_approval' "
        "AND to_version = :v ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "a": agent_key, "e": dst, "v": checks["version"]}).scalar()
    if pending:
        return {"status": "awaiting_approval", "decision_id": pending, "checks": checks,
                "already_asked": True}
    from .core import create_blocking_decision

    question = (f"Promote {agent_key} v{checks['version']} to {dst}"
                + (f" (replacing v{checks['live_version']})" if checks["live_version"] else "")
                + "?")
    recommendation = {"action": ACTION, "agent_key": agent_key, "environment": dst,
                      "from_environment": checks["from_environment"],
                      "version": checks["version"], "from_version": checks["live_version"],
                      "runner": runner, "requested_by": actor,
                      "checks": [{k: c[k] for k in ("key", "ok", "label")}
                                 for c in checks["checks"]],
                      "in_flight": checks["in_flight"]["note"]}
    decision_id = create_blocking_decision(
        connection, project_id, None, question, recommendation, {},
        "agent_deployment_promotion", risk_tier=2, default_action="hold")
    _history(connection, project_id=project_id, agent_key=agent_key, environment=dst,
             action="promote", actor=actor, from_version=checks["live_version"],
             to_version=checks["version"], status="awaiting_approval", checks=checks,
             decision_id=decision_id)
    emit_event(connection, project_id, "agent.deployment.promotion_requested", actor,
               "decision", decision_id, {"agent_key": agent_key, "environment": dst,
                                         "version": checks["version"]})
    return {"status": "awaiting_approval", "decision_id": decision_id, "checks": checks}


def on_promotion_answered(connection: Connection, decision: Any, *, verdict: str,
                          actor: str) -> dict:
    """The follow-on of a `promote_agent_deployment` Decision (decisions.py): approve applies
    exactly the version the card named; reject records it."""
    rec = _json(decision.get("recommendation"), {}) or {}
    project_id = str(decision["project_id"])
    key, env, version = str(rec.get("agent_key")), str(rec.get("environment")), rec.get("version")
    status = "applied" if verdict == "approve" else "rejected"
    connection.execute(text(
        "UPDATE agent_deployment_history SET status = :s, applied_at = CASE WHEN :s = 'applied' "
        "THEN now() END WHERE decision_id = :d AND status = 'awaiting_approval'"),
        {"s": status, "d": decision["id"]})
    if verdict != "approve":
        return {"applied": False}
    out = deploy(connection, project_id, key, env, actor=actor, version=int(version),
                 runner=str(rec.get("runner") or "deployed_agent"), action="promote")
    if rec.get("from_environment"):
        out = _carry_runtime(connection, project_id, key, str(rec["from_environment"]), env,
                             actor=actor) or out
    return {"applied": True, "deployment": out}


# ── Placement: runtime local | microvm (P8a, boss-agent-hosts-lld.md §2.4, FR-1, FR-2) ─────────

RUNTIMES = ("local", "microvm")
VM_MODES = ("replica", "per_run")
#: Production on microVMs is HA: at least two replicas on two hosts (NFR-1).
PROD_MIN_REPLICAS = 2
MAX_REPLICAS = 64


def set_runtime(connection: Connection, project_id: str, agent_key: str, environment: str, *,
                actor: str, runtime: str, vm_mode: Optional[str] = None,
                replicas: Optional[int] = None, resources: Optional[dict] = None,
                image_digest: Optional[str] = None, placement: Optional[dict] = None,
                settings: Any = None, action: str = "host") -> dict:
    """Where this deployment's runs execute. `local` (the default) is the worker path, unchanged;
    `microvm` places replicas on enrolled hosts (the scheduler, services/fleet_scheduler.py).
    Runs already admitted keep the placement they were admitted with; new runs take this one.
    `per_run` is built but OFF while BOSS_MICROVM_CLAIMS_FROZEN (operator, 2026-11-14)."""
    env = _env(environment)
    current = get(connection, project_id, agent_key, env)
    if current is None:
        raise DeploymentError(f"{agent_key} is not deployed to {env}.", status=404)
    if runtime not in RUNTIMES:
        raise DeploymentError(f"runtime must be one of {', '.join(RUNTIMES)}.")
    if runtime == "local":
        mode, count = None, 1
    else:
        mode = vm_mode or current.get("vm_mode") or "replica"
        if mode not in VM_MODES:
            raise DeploymentError(f"vm_mode must be one of {', '.join(VM_MODES)}.")
        if mode == "per_run":
            from .fleet_scheduler import frozen

            if frozen(settings):
                raise DeploymentError(
                    "per_run VMs are built but off on this install: BOSS_MICROVM_CLAIMS_FROZEN "
                    "is set. Use vm_mode replica, or ask the operator to unfreeze microVM "
                    "claims.", status=409)
        floor = PROD_MIN_REPLICAS if (env == "prod" and mode == "replica") else 1
        count = int(replicas if replicas is not None else max(
            floor, int(current.get("replicas") or 1)))
        if not floor <= count <= MAX_REPLICAS:
            raise DeploymentError(
                f"replicas must be between {floor} and {MAX_REPLICAS}"
                + (" (production on microVMs runs at least two replicas on two hosts)"
                   if floor > 1 else "") + ".")
    connection.execute(text("""
        UPDATE agent_deployments SET runtime = :rt, vm_mode = :m, replicas = :n,
               resources = COALESCE(CAST(:res AS JSONB), resources),
               image_digest = COALESCE(:img, image_digest),
               placement = COALESCE(CAST(:pl AS JSONB), placement),
               placement_state = CASE WHEN :rt = 'local' THEN NULL ELSE 'pending' END,
               updated_at = now()
         WHERE id = :id"""),
        {"rt": runtime, "m": mode, "n": count,
         "res": json.dumps(resources) if resources is not None else None, "img": image_digest,
         "pl": json.dumps(placement) if placement is not None else None, "id": current["id"]})
    _history(connection, project_id=project_id, agent_key=agent_key, environment=env,
             action=action, actor=actor, from_version=current.get("version"),
             to_version=current.get("version"),
             checks={"runtime": runtime, "vm_mode": mode, "replicas": count,
                     "from_runtime": current.get("runtime") or "local"})
    emit_event(connection, project_id, "agent.deployment.runtime", actor, "agent_deployment",
               current["id"], {"agent_key": agent_key, "environment": env, "runtime": runtime,
                               "vm_mode": mode, "replicas": count})
    return by_id(connection, current["id"]) or {}


def _carry_runtime(connection: Connection, project_id: str, agent_key: str, src: str, dst: str,
                   *, actor: str) -> Optional[dict]:
    """A promotion carries the source environment's placement with its version (FR-1)."""
    source = get(connection, project_id, agent_key, src)
    target = get(connection, project_id, agent_key, dst)
    if source is None or target is None:
        return None
    if (source.get("runtime") or "local") == (target.get("runtime") or "local") \
            and source.get("vm_mode") == target.get("vm_mode"):
        return None
    replicas = None
    if (source.get("runtime") or "local") == "microvm":
        floor = PROD_MIN_REPLICAS if dst == "prod" and (source.get("vm_mode") or "replica") \
            == "replica" else 1
        replicas = max(floor, int(target.get("replicas") or 1), int(source.get("replicas") or 1))
    return set_runtime(connection, project_id, agent_key, dst, actor=actor,
                       runtime=source.get("runtime") or "local", vm_mode=source.get("vm_mode"),
                       replicas=replicas, resources=source.get("resources"),
                       image_digest=source.get("image_digest"),
                       placement=source.get("placement"), action="promote")


# ── What the screens draw ──────────────────────────────────────────────────────────────────────

def _run_stats(connection: Connection, project_id: str, *, days: int = 7) -> dict:
    """Per (agent_key, environment): runs, ok, failed, cost and the last run, over `days`."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = connection.execute(text("""
        SELECT r.agent_key, r.environment, count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'completed') AS ok,
               count(*) FILTER (WHERE r.status = 'failed') AS failed,
               max(r.created_at) AS last_run_at,
               COALESCE(sum((SELECT sum(l.cost_usd_micros) FROM llm_calls l
                              WHERE l.run_id = r.id)), 0) AS cost
          FROM agent_runs r
         WHERE r.project_id = :p AND r.agent_key IS NOT NULL AND r.created_at >= :since
         GROUP BY r.agent_key, r.environment"""), {"p": project_id, "since": since}
    ).mappings().all()
    return {(r["agent_key"], r["environment"]): dict(r) for r in rows}


def environments_view(connection: Connection, project_id: str, agent_key: str) -> dict:
    """Wireframe A3: one column per environment — its live version, how it is doing, what can
    happen next — the draft that is not deployed anywhere, where it runs, and the history."""
    from ..agents import versions as V

    deployed = {d["environment"]: d for d in for_agent(connection, project_id, agent_key)}
    stats = _run_stats(connection, project_id)
    all_versions = V.list_versions(connection, project_id, agent_key)
    latest = all_versions[0]["version"] if all_versions else None
    live_versions = {d.get("version") for d in deployed.values()}
    columns = []
    for env in ENVIRONMENTS:
        dep = deployed.get(env)
        stat = stats.get((agent_key, env)) or {}
        finished = int(stat.get("ok") or 0) + int(stat.get("failed") or 0)
        column: dict[str, Any] = {"environment": env, "deployed": dep is not None}
        if dep is not None:
            column.update({
                "deployment_id": dep["id"], "version": dep.get("version"), "state": "live",
                "previous_version": dep.get("previous_version"),
                "host": {"id": dep.get("host_id"), "label": dep.get("host_label")},
                "runs_7d": int(stat.get("runs") or 0),
                "ok_rate": round(int(stat.get("ok") or 0) / finished, 4) if finished else None,
                "cost_7d_usd": round(int(stat.get("cost") or 0) / 1e6, 2),
                "in_flight": in_flight(connection, project_id, agent_key, env, dep.get("version")),
                "promoted_at": dep.get("promoted_at"), "promoted_by": dep.get("promoted_by"),
            })
        nxt = ENVIRONMENTS[ENVIRONMENTS.index(env) + 1] if env != "prod" else None
        column["next"] = ({"action": "promote", "to": nxt} if dep is not None and nxt
                          else {"action": "rollback", "to_version": dep.get("previous_version")}
                          if dep is not None and dep.get("previous_version") else None)
        columns.append(column)
    draft = None
    if latest is not None and latest not in live_versions:
        draft = {"version": latest, "state": "draft",
                 "note": "Not deployed anywhere yet; deploy it to dev to try it."}
    return {"agent_key": agent_key, "environments": columns, "draft": draft,
            "versions": [{"version": v["version"], "active": v["active"],
                          "content_hash": v["content_hash"],
                          "environments": [d["environment"] for d in deployed.values()
                                           if d.get("version") == v["version"]]}
                         for v in all_versions],
            "history": history(connection, project_id, agent_key)}


def _kind_of(connection: Connection, deployment_ids: list) -> dict:
    """bot when a deployment has a trigger (§4.4: a bot is a deployment with at least one
    non-chat trigger), with the first trigger's words for the A1 subtitle."""
    if not deployment_ids:
        return {}
    rows = connection.execute(text(
        "SELECT deployment_id, kind, name, config, status FROM agent_triggers "
        "WHERE deployment_id = ANY(:d) ORDER BY created_at"), {"d": deployment_ids}
    ).mappings().all()
    out: dict = {}
    for r in rows:
        out.setdefault(r["deployment_id"], []).append(dict(r))
    return out


def overview(connection: Connection, project_id: str, *, environment: Optional[str] = None,
             kind: Optional[str] = None) -> dict:
    """Wireframe A1: every agent and bot, the version live per environment, health, 7-day cost
    and the last run; and the four tiles. System services are NOT here (D7): they are under
    health."""
    from . import agent_health, agent_triggers

    rows = connection.execute(text(
        "SELECT * FROM agent_deployments WHERE project_id = :p AND status = 'live' "
        "ORDER BY agent_key, environment"), {"p": project_id}).mappings().all()
    deployments = [_out(r) for r in rows]
    triggers = _kind_of(connection, [d["id"] for d in deployments])
    stats = _run_stats(connection, project_id)
    health = {(h["agent_key"], h["environment"]): h
              for h in agent_health.deployments(connection, project_id, hours=24)}
    agents: dict[str, dict] = {}
    for dep in deployments:
        entry = agents.setdefault(dep["agent_key"], {
            "key": dep["agent_key"], "name": dep["display_name"] or dep["agent_key"],
            "initials": "".join(w[:1] for w in (dep["display_name"] or dep["agent_key"])
                                .replace("_", " ").split()[:2]).upper(),
            "kind": "assistant", "kind_detail": None, "live": [], "health": None,
            "cost_7d_usd": 0.0, "last_run_at": None, "runner": dep["runner"]})
        entry["live"].append({"environment": dep["environment"], "version": dep.get("version"),
                              "deployment_id": dep["id"]})
        trig = triggers.get(dep["id"]) or []
        if trig:
            entry["kind"] = "bot"
            entry["kind_detail"] = entry["kind_detail"] or agent_triggers.short_words(trig[0])
        stat = stats.get((dep["agent_key"], dep["environment"])) or {}
        entry["cost_7d_usd"] = round(entry["cost_7d_usd"] + int(stat.get("cost") or 0) / 1e6, 2)
        last = _iso(stat.get("last_run_at"))
        if last and (entry["last_run_at"] is None or last > entry["last_run_at"]):
            entry["last_run_at"] = last
        h = health.get((dep["agent_key"], dep["environment"]))
        if h is not None and (entry["health"] is None or dep["environment"] == "prod"):
            entry["health"] = {"state": h["state"], "label": h["label"]}
    out = list(agents.values())
    for entry in out:
        entry["health"] = entry["health"] or {"state": "idle", "label": "No runs yet"}
        if entry["kind"] == "assistant":
            entry["kind_detail"] = "Ask BOSS, panels, the API"
    if environment:
        env = _env(environment)
        out = [a for a in out if any(x["environment"] == env for x in a["live"])]
    if kind:
        out = [a for a in out if a["kind"] == kind]
    spend = sum(round(int(s.get("cost") or 0) / 1e6, 4) for s in stats.values())
    bots_running = sum(1 for a in out if a["kind"] == "bot" and a["health"]["state"] != "paused")
    attention = sum(1 for a in out if a["health"]["state"] in ("failing", "stuck"))
    return {"summary": {"live_agents": len(out), "bots_running": bots_running,
                        "need_attention": attention, "spend_7d_usd": round(spend, 2)},
            "agents": out,
            "note": "System services (reaper, backups, doctor) are listed under Health, not "
                    "here: they aren't agents."}


__all__ = ["ACTION", "APPROVAL_ENVIRONMENTS", "DeploymentError", "ENVIRONMENTS", "RUNNERS",
           "admit_run", "by_id", "cost_per_run", "deploy", "environments_view", "for_agent", "get",
           "history", "in_flight", "live_for_run", "on_promotion_answered", "overview",
           "promote", "promotion_checks", "rollback", "set_host"]
