"""PER-DEPLOYMENT AGENT HEALTH (AP-P3, BOSSAPP-1054; AGENT_PLATFORM §4.4 "Health"; wireframe A5).

"Is every bot alive, fast and within budget?" — per agent deployment, over a window, from the
run table (agent_runs rows carry their deployment since AP-P3):

  state        healthy | failing (failed in a row) | paused (every trigger paused) | stuck (a
               run running with no progress for 15 minutes) | idle (no runs)
  ok_rate      completed / (completed + failed)
  p95_ms       the 95th percentile of finished runs' wall time
  cost_usd     llm_calls of those runs
  last_run_at  and the last failure, with its cause IN PLAIN WORDS ("the Grafana token was
               rejected") and how many failures before it had the same cause
  interrupts   runs waiting on a person (the interrupt backlog)

THE SYSTEM DOCTOR READS `failing_bots`: a bot whose trigger failed FAILING_STREAK times in a row
is a finding ("On-call SRE failed 3 times in a row"), and its circuit breaker pauses the trigger
on its own (`pause_failing`) when the trigger allows it (auto_pause). Pausing stops new firings
and their spend; it never stops a run in flight.

(App deployments' health — is the deployed APP serving — is services/deployment_health.py.)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

#: How many failures in a row make a bot "failing" (and a doctor finding).
FAILING_STREAK = 3
STUCK_AFTER = timedelta(minutes=15)

_SERVICES = ("grafana", "github", "gitlab", "slack", "jira", "linear", "pagerduty", "datadog",
             "openai", "anthropic", "openrouter", "azure", "aws", "gcp", "sentry", "postgres",
             "mattermost", "plane", "zendesk", "stripe")
_PROPER = {"github": "GitHub", "gitlab": "GitLab", "openai": "OpenAI", "pagerduty": "PagerDuty",
           "openrouter": "OpenRouter", "aws": "AWS", "gcp": "GCP"}
_REJECTED = re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|(token|key|credential)s?\b.{0,40}"
                       r"(rejected|invalid|expired|revoked|denied)|invalid[_ ](token|api[_ ]key)|"
                       r"bad credentials|authentication failed", re.I)
_TIMEOUT = re.compile(r"time[d ]?out|deadline exceeded|ReadTimeout|ConnectTimeout", re.I)
_RATE = re.compile(r"\b429\b|rate[- ]?limit|too many requests", re.I)
_UNREACHABLE = re.compile(r"connection refused|name or service not known|could not resolve|"
                          r"nodename nor servname|unreachable|ConnectError", re.I)
_BUDGET = re.compile(r"budget|spend(ing)? cap|over (its|the) cap|out of credit", re.I)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _service_in(error: str) -> Optional[str]:
    low = error.lower()
    for name in _SERVICES:
        if name in low:
            return _PROPER.get(name, name.capitalize())
    return None


def plain_cause(error: Optional[str]) -> Optional[str]:
    """A run's failure as a person would say it: "the Grafana token was rejected", "it timed out
    waiting for GitHub". The raw error stays on the run (and its trace) for whoever needs it."""
    if not error:
        return None
    raw = str(error)
    service = _service_in(raw)
    if _REJECTED.search(raw):
        return f"the {service} token was rejected" if service else "a credential was rejected"
    if _RATE.search(raw):
        return f"{service} rate-limited it" if service else "it was rate-limited"
    if _TIMEOUT.search(raw):
        return f"it timed out waiting for {service}" if service else "it timed out"
    if _UNREACHABLE.search(raw):
        return f"it could not reach {service}" if service else "it could not reach a service"
    if _BUDGET.search(raw):
        return "it hit its spending cap"
    first = raw.strip().splitlines()[0] if raw.strip() else raw
    first = re.sub(r"^[A-Za-z_.]+(Error|Exception):\s*", "", first)
    return f"it failed: {first[:160]}"


def recent_failures(connection: Any, deployment_id: str, *, limit: int = 5) -> list[dict]:
    return [dict(r) for r in connection.execute(text(
        "SELECT id, error, updated_at AS at FROM agent_runs WHERE deployment_id = :d "
        "AND status = 'failed' ORDER BY updated_at DESC, id DESC LIMIT :n"),
        {"d": deployment_id, "n": int(limit)}).mappings().all()]


def streak(connection: Any, deployment_id: str) -> int:
    """Failures in a row, newest first, among the deployment's ended runs."""
    statuses = [r[0] for r in connection.execute(text(
        "SELECT status FROM agent_runs WHERE deployment_id = :d AND status IN "
        "('completed', 'failed') ORDER BY updated_at DESC, id DESC LIMIT 20"),
        {"d": deployment_id}).all()]
    n = 0
    for s in statuses:
        if s != "failed":
            break
        n += 1
    return n


def deployments(connection: Any, project_id: str, *, hours: int = 24) -> list[dict]:
    """The A5 rows: one per live deployment (see the module docstring)."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    deps = [dict(r) for r in connection.execute(text(
        "SELECT * FROM agent_deployments WHERE project_id = :p AND status = 'live' "
        "ORDER BY agent_key, environment"), {"p": project_id}).mappings().all()]
    if not deps:
        return []
    ids = [d["id"] for d in deps]
    stats = {r["deployment_id"]: dict(r) for r in connection.execute(text("""
        SELECT r.deployment_id, count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'completed') AS ok,
               count(*) FILTER (WHERE r.status = 'failed') AS failed,
               count(*) FILTER (WHERE r.status = 'awaiting_approval') AS waiting,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY
                 EXTRACT(EPOCH FROM (r.updated_at - r.created_at)) * 1000)
                 FILTER (WHERE r.status IN ('completed', 'failed')) AS p95_ms,
               max(r.created_at) AS last_run_at,
               COALESCE(sum((SELECT sum(l.cost_usd_micros) FROM llm_calls l
                              WHERE l.run_id = r.id)), 0) AS cost
          FROM agent_runs r WHERE r.deployment_id = ANY(:d) AND r.created_at >= :since
         GROUP BY r.deployment_id"""), {"d": ids, "since": since}).mappings()}
    stuck = {r["deployment_id"]: int(r["n"]) for r in connection.execute(text(
        "SELECT deployment_id, count(*) AS n FROM agent_runs WHERE deployment_id = ANY(:d) "
        "AND status = 'running' AND updated_at < :t GROUP BY deployment_id"),
        {"d": ids, "t": datetime.now(timezone.utc) - STUCK_AFTER}).mappings()}
    triggers: dict = {}
    for r in connection.execute(text(
            "SELECT id, deployment_id, kind, status, paused_reason, paused_by, failure_streak "
            "FROM agent_triggers WHERE deployment_id = ANY(:d) ORDER BY created_at"),
            {"d": ids}).mappings():
        triggers.setdefault(r["deployment_id"], []).append(dict(r))
    out = []
    for dep in deps:
        stat = stats.get(dep["id"]) or {}
        ok, failed = int(stat.get("ok") or 0), int(stat.get("failed") or 0)
        trig = triggers.get(dep["id"]) or []
        paused = bool(trig) and all(t["status"] == "paused" for t in trig)
        failures = recent_failures(connection, dep["id"], limit=FAILING_STREAK + 2)
        in_a_row = streak(connection, dep["id"])
        last_failure = None
        if failures:
            causes = [plain_cause(f["error"]) for f in failures]
            same = 1
            for cause in causes[1:]:
                if cause != causes[0]:
                    break
                same += 1
            last_failure = {"run_id": failures[0]["id"], "at": _iso(failures[0]["at"]),
                            "cause": causes[0], "same_cause_count": same,
                            "error": (failures[0]["error"] or "")[:300] or None}
        if paused:
            state, label = "paused", "Paused"
        elif stuck.get(dep["id"]):
            state, label = "stuck", f"{stuck[dep['id']]} stuck"
        elif in_a_row:
            state, label = "failing", f"{in_a_row} failure{'s' if in_a_row != 1 else ''}"
        elif not stat:
            state, label = "idle", "No runs yet"
        else:
            state, label = "healthy", "Healthy"
        out.append({
            "deployment_id": dep["id"], "agent_key": dep["agent_key"],
            "name": dep["display_name"] or dep["agent_key"], "environment": dep["environment"],
            "version": dep.get("version"), "runner": dep["runner"],
            "kind": "bot" if trig else "assistant",
            "state": state, "label": label, "runs": int(stat.get("runs") or 0), "ok": ok,
            "failed": failed, "failure_streak": in_a_row,
            "ok_rate": round(ok / (ok + failed), 4) if (ok + failed) else None,
            "p95_ms": int(stat.get("p95_ms") or 0),
            "cost_usd": round(int(stat.get("cost") or 0) / 1e6, 2),
            "last_run_at": _iso(stat.get("last_run_at")),
            "interrupt_backlog": int(stat.get("waiting") or 0),
            "stuck_runs": int(stuck.get(dep["id"]) or 0),
            "paused": paused,
            "paused_reason": next((t["paused_reason"] for t in trig if t["status"] == "paused"),
                                  None),
            "triggers": [{"id": t["id"], "kind": t["kind"], "status": t["status"]} for t in trig],
            "last_failure": last_failure,
            "actions": (["resume"] if paused else ["pause"] if trig else [])
            + (["open_failures"] if failed else []),
        })
    return out


def alerts(rows: list[dict]) -> list[dict]:
    """The A5 callouts: a failing bot in one sentence, with the trace of its last failure."""
    out = []
    for row in rows:
        failure = row.get("last_failure") or {}
        if row["failure_streak"] < FAILING_STREAK or not failure:
            continue
        same = failure.get("same_cause_count", 1) >= FAILING_STREAK
        detail = (f"Same cause each time: {failure['cause']}." if same
                  else f"Last cause: {failure['cause']}.")
        if row["paused"]:
            detail += " BOSS paused its trigger."
        out.append({"deployment_id": row["deployment_id"], "agent_key": row["agent_key"],
                    "title": f"{row['name']} failed {row['failure_streak']} times in a row",
                    "detail": detail, "since": failure.get("at"),
                    "trace_run_id": failure.get("run_id")})
    return out


def failing_bots(connection: Any) -> list[dict]:
    """Install-wide, for the system doctor: every trigger still on whose runs failed
    FAILING_STREAK times in a row, with its deployment's name and the plain cause."""
    rows = connection.execute(text("""
        SELECT t.id AS trigger_id, t.project_id, t.kind, t.failure_streak, t.auto_pause,
               d.id AS deployment_id, d.agent_key, d.display_name, d.environment
          FROM agent_triggers t JOIN agent_deployments d ON d.id = t.deployment_id
         WHERE t.status = 'on' AND t.failure_streak >= :n ORDER BY t.id"""),
        {"n": FAILING_STREAK}).mappings().all()
    out = []
    for r in rows:
        failures = recent_failures(connection, r["deployment_id"], limit=1)
        out.append({**dict(r), "cause": plain_cause(failures[0]["error"]) if failures else None,
                    "last_run_id": failures[0]["id"] if failures else None})
    return out


def pause_failing(connection: Any, bot: dict) -> str:
    """The doctor's automatic remedy: pause the failing trigger (never a run in flight)."""
    from . import agent_triggers

    if not bot.get("auto_pause"):
        return "auto-pause is off for this trigger; a person decides"
    agent_triggers.set_paused(
        connection, project_id=bot["project_id"], trigger_id=bot["trigger_id"], actor="boss",
        paused=True, reason=(f"Failed {bot['failure_streak']} times in a row"
                             + (f": {bot['cause']}" if bot.get("cause") else "")
                             + ". The system doctor paused it; resume when it is fixed."))
    return f"paused the {bot['kind']} trigger of {bot['display_name'] or bot['agent_key']}"


def view(connection: Any, project_id: str, *, hours: int = 24) -> dict:
    """The whole A5 screen: bots and agents, system services (D7), workers, the callouts."""
    from . import system_services
    from .worker_registry import scaling_signal

    rows = deployments(connection, project_id, hours=hours)
    return {"hours": hours, "deployments": rows, "alerts": alerts(rows),
            "system_services": system_services.listing(connection),
            "workers": scaling_signal(connection, project_id)}


__all__ = ["FAILING_STREAK", "alerts", "deployments", "failing_bots", "pause_failing",
           "plain_cause", "recent_failures", "streak", "view"]
