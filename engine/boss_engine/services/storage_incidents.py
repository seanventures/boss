"""STORAGE INCIDENTS: one object drives every surface (LLD §4, §5, §6; design decision 5).

The watch says a level; this module turns it into ONE incident per host — opened at warn (tier 1
cleanup, no hold), escalated at critical (hold, tiers 1–3, the strip flashes), `asking` while a
tier-3 card waits, `resuming` once the target is reached, `resolved` with a report. The console
never reads prose: `flow` is a StripNode[] (type/label ≤ 4 words/badges, with a key, a state and
a time), drawn by FlowThumb on cards and FlowCanvas on the report.

Learning (§4.4): every incident gets a deterministic `cause_key`; one seen twice in 30 days
proposes a rule (a decision of action `storage_rule`); approving it writes `retention_rules` and
a KG lesson, so next time the same cause is tier 2, automatic.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import storage_rules, work_hold
from .core import emit_event, make_id

_log = logging.getLogger("boss.storage_incidents")

STATUSES = ("warn", "critical", "asking", "resuming", "resolved")
FLOW_KEYS = ("detected", "paused", "cleaning", "asking", "freed", "resumed", "reported")
MAX_LABEL_WORDS = 4
#: A cause seen this many times in the window proposes a rule.
LEARN_AFTER = 2
LEARN_WINDOW_DAYS = 30
AGENT_KEY = "infra_agent"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def default_project(connection: Any) -> Optional[str]:
    try:
        return connection.execute(text(
            "SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
    except Exception:  # noqa: BLE001
        return None


def short(label: str) -> str:
    """≤ MAX_LABEL_WORDS words (read_model._strip's rule): the console draws nodes, not prose."""
    words = str(label or "").split()
    return " ".join(words[:MAX_LABEL_WORDS])


# ── the deterministic explainer ─────────────────────────────────────────────────────────────

def explain_cause(sample: Mapping[str, Any], forecast: Any,
                  thresholds: Mapping[str, float]) -> tuple[str, str]:
    """(cause_key, one plain sentence). The biggest avoidable tenant wins; growth when nothing
    stands out; `disk.low` as the floor."""
    total = _f(sample.get("total_gb")) or 0.0
    backups = _f(sample.get("backups_gb")) or 0.0
    backups_n = int(sample.get("backups_n") or 0)
    pg_temp = _f(sample.get("pg_temp_gb")) or 0.0
    images = _f(sample.get("images_gb")) or 0.0
    cache = _f(sample.get("cache_gb")) or 0.0
    logs = _f(sample.get("logs_gb")) or 0.0
    alloc, used = _f(sample.get("docker_alloc_gb")), _f(sample.get("docker_used_gb"))
    unreclaimed = (alloc - used) if alloc is not None and used is not None else 0.0
    candidates: list[tuple[float, str, str]] = []
    if pg_temp > thresholds.get("pg_temp_critical_gb", 5.0):
        candidates.append((pg_temp * 3, "pg.temp_spill",
                           f"Postgres wrote {pg_temp:.0f} GB of temporary files: a query ran with "
                           "no temp-space limit."))
    if backups >= 1:
        candidates.append((backups, "backups.no_retention",
                           f"{backups_n or 'Many'} backups hold {backups:.0f} GB; one is written per "
                           "upgrade and nothing removed the old ones."))
    if unreclaimed > 10:
        candidates.append((unreclaimed, "docker.vm_unreclaimed",
                           f"Docker's disk file holds {unreclaimed:.0f} GB it no longer uses; Docker "
                           "Desktop does not hand space back to macOS on its own."))
    if images + cache > 10:
        candidates.append((images + cache, "images.no_retention",
                           f"Old BOSS images and build cache take {images + cache:.0f} GB; each "
                           "upgrade adds a set and keeps the last."))
    if logs > 2:
        candidates.append((logs, "logs.uncapped", f"Container logs grew to {logs:.0f} GB with no cap."))
    if candidates:
        candidates.sort(key=lambda c: c[0], reverse=True)
        return candidates[0][1], candidates[0][2]
    growth = float(getattr(forecast, "growth_gb_per_day", 0.0) or 0.0)
    days = getattr(forecast, "days_until_full", None)
    if growth > 0 and days is not None:
        return "growth.fast", (f"The disk grows {growth:.1f} GB a day; at that rate it is full in "
                               f"{days:.0f} days.")
    free_pct = _free_pct(sample)
    return "disk.low", (f"{free_pct:.0f} % of the disk is free" if free_pct is not None
                        else "The disk is nearly full") + f" (of {total:.0f} GB)."


def _free_pct(sample: Mapping[str, Any]) -> Optional[float]:
    from .storage_watch import free_pct_of

    return free_pct_of(sample)


# ── rows ────────────────────────────────────────────────────────────────────────────────────

def _row(connection: Any, incident_id: str, *, lock: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE id = :id" + (" FOR UPDATE" if lock else "")),
        {"id": incident_id}).mappings().first()
    return dict(row) if row else None


def current_for_host(connection: Any, host_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE host_id = :h AND status <> 'resolved'"),
        {"h": host_id}).mappings().first()
    return dict(row) if row else None


def open_rows(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE status <> 'resolved' "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, opened_at")).mappings().all()
    return [dict(r) for r in rows]


def get(connection: Any, incident_id: str) -> Optional[dict[str, Any]]:
    return _row(connection, incident_id)


def level_of(incident: Optional[Mapping[str, Any]]) -> str:
    if not incident or incident.get("status") == "resolved":
        return "ok"
    return "critical" if incident.get("severity") == "critical" else "warn"


def activity_text(incident: Mapping[str, Any]) -> str:
    status = incident.get("status")
    if status == "resolved":
        return "Storage recovered"
    if status == "asking":
        return "Infra Agent needs one answer"
    if status == "resuming":
        return "Infra Agent is resuming work"
    reachable = bool((incident.get("helper") or {}).get("reachable"))
    plan = _json(incident.get("plan"), []) or []
    if any(step.get("state") == "running" and (step.get("where") == "api" or reachable)
           for step in plan):
        return "Infra Agent is cleaning"
    if not reachable:
        return "Cleanup blocked: host helper unavailable"
    if any(step.get("state") in ("planned", "queued") for step in plan):
        return "Cleanup queued"
    return "Cleanup exhausted: free space by hand"


def last_resolved(connection: Any, host_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, resolved_at, freed_gb, cause_key FROM storage_incidents "
        "WHERE host_id = :h AND status = 'resolved' ORDER BY resolved_at DESC LIMIT 1"),
        {"h": host_id}).mappings().first()
    if row is None:
        return None
    return {"incident_id": row["id"], "at": _iso(row["resolved_at"]),
            "freed_gb": round(float(row["freed_gb"] or 0), 2), "cause_key": row["cause_key"]}


def _update(connection: Any, incident_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets, params = [], {"id": incident_id}
    for k, v in fields.items():
        if k in ("plan", "flow", "report"):
            sets.append(f"{k} = CAST(:{k} AS JSONB)")
            params[k] = json.dumps(v, default=str)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    connection.execute(text(f"UPDATE storage_incidents SET {', '.join(sets)}, updated_at = now() "
                            "WHERE id = :id"), params)


# ── the flow the console draws ──────────────────────────────────────────────────────────────

def flow_of(inc: Mapping[str, Any], *, paused_runs: Optional[int] = None) -> list[dict[str, Any]]:
    """IncidentFlowNode[] (docs/infra/INFRA_AGENT_API.md §2): node order is the flow."""
    plan = _json(inc.get("plan"), []) or []
    status = str(inc.get("status") or "warn")
    severity = str(inc.get("severity") or "warn")
    paused = int(paused_runs if paused_runs is not None else inc.get("paused_runs") or 0)
    freed = float(inc.get("freed_gb") or 0.0)
    resolved = status == "resolved"
    asking = status == "asking" or any(s.get("state") == "ask" for s in plan)
    cleaning_active = status in ("warn", "critical") and any(
        s.get("state") in ("planned", "queued", "running") for s in plan)
    done_steps = [s for s in plan if s.get("state") == "done"]
    free_now = _f(inc.get("free_pct_now"))
    target = _f(inc.get("target_pct")) or 25.0
    reached = resolved or status == "resuming" or (free_now is not None and free_now >= target)
    held = bool(inc.get("hold_since"))
    released = bool(inc.get("hold_released_at"))
    free_start = _f(inc.get("free_pct_start"))
    cause = str(inc.get("cause_text") or "")

    def node(key: str, type_: str, label: str, state: str, at: Any, detail: str,
             badges: Optional[list[str]] = None, icon: Optional[str] = None) -> dict[str, Any]:
        out = {"key": key, "type": type_, "label": short(label), "badges": (badges or [])[:2],
               "state": state, "at": _iso(at), "detail": detail[:300]}
        if icon:
            out["icon"] = icon
        return out

    nodes = [node("detected", "signal", f"{severity.title()} detected", "done", inc.get("opened_at"),
                  cause or f"{free_start:.0f} % free" if free_start is not None else cause or "Storage pressure",
                  [f"{free_start:.0f} % free"] if free_start is not None else [], "HardDrive")]
    if held or severity == "critical":
        nodes.append(node("paused", "action", f"Paused {paused} runs" if paused else "Paused new work",
                          "done" if held else "waiting", inc.get("hold_since"),
                          "No new work is claimed; running steps hand back at their next "
                          "checkpoint. Nothing is killed, nothing is lost.",
                          ["checkpoint", "nothing lost"], "PauseCircle"))
    n_total = len([s for s in plan if s.get("tier", 3) <= 2])
    n_done = len([s for s in done_steps if s.get("tier", 3) <= 2])
    nodes.append(node("cleaning", "agent", "Cleaning safest first",
                      "done" if (reached or (plan and not cleaning_active and not asking)) else
                      ("active" if cleaning_active else "waiting"),
                      (plan[0].get("started_at") if plan else None) or inc.get("opened_at"),
                      f"Step {min(n_done + 1, max(n_total, 1))} of {max(n_total, 1)}: rebuildable "
                      "things first, then items past an approved rule; each step dry-run, "
                      "receipted.", [f"{n_done}/{n_total} steps", f"{freed:.1f} GB"], "Broom"))
    ask_steps = [s for s in plan if s.get("tier") == 3]
    if ask_steps:
        s = ask_steps[0]
        ask_state = "ask" if s.get("state") == "ask" else (
            "done" if s.get("state") in ("done", "running", "queued") else "waiting")
        nodes.append(node("asking", "needs_you", "Retention needs you" if ask_state == "ask"
                          else "Retention approved" if ask_state == "done" else "Retention if needed",
                          ask_state, s.get("asked_at"),
                          f"{s.get('label', 'Irreversible step')}: {s.get('items') or 0} items, "
                          f"{(s.get('gb') or 0):.1f} GB. Never automatic without a rule.",
                          [f"{(s.get('gb') or 0):.0f} GB", "ask once"], "ShieldCheck"))
    nodes.append(node("freed", "outcome", f"Freed {freed:.0f} GB" if freed >= 1 else "Freed space",
                      "done" if reached else "waiting", inc.get("hold_released_at") if reached else None,
                      f"Target {target:.0f} % free; now {free_now:.0f} %." if free_now is not None
                      else f"Target {target:.0f} % free.", [f"target {target:.0f} %"], "ArrowsIn"))
    if held or severity == "critical":
        nodes.append(node("resumed", "action", "Work resumed", "done" if released else "waiting",
                          inc.get("hold_released_at"),
                          "Paused work resumes oldest first, from its checkpoint.",
                          ["oldest first"], "PlayCircle"))
    nodes.append(node("reported", "outcome", "Report posted", "done" if resolved else "waiting",
                      inc.get("resolved_at"), "Cause, what was removed, what was paused, what "
                      "BOSS learned.", [], "FileText"))
    return nodes


def public(connection: Any, inc: Mapping[str, Any]) -> dict[str, Any]:
    """The Incident shape (API doc §2)."""
    from . import host_jobs
    from .storage_watch import HELPER_UNREACHABLE_AFTER

    hold = work_hold.state(connection)
    is_held = bool(hold.get("active") and hold.get("incident_id") == inc["id"])
    paused = work_hold.paused_count(connection) if is_held else int(inc.get("paused_runs") or 0)
    seen = host_jobs.last_seen(connection, str(inc["host_id"]))
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    reachable = seen is not None and (_now() - seen) < HELPER_UNREACHABLE_AFTER
    return {"id": inc["id"], "host_id": inc["host_id"],
            "host_label": inc.get("host_label") or inc["host_id"],
            "status": inc["status"], "severity": inc.get("severity") or "warn",
            "opened_at": _iso(inc.get("opened_at")), "resolved_at": _iso(inc.get("resolved_at")),
            "cause_key": inc.get("cause_key"), "cause_text": inc.get("cause_text") or "",
            "free_pct_start": _f(inc.get("free_pct_start")), "free_pct_now": _f(inc.get("free_pct_now")),
            "target_pct": _f(inc.get("target_pct")) or 25.0,
            "freed_gb": round(float(inc.get("freed_gb") or 0), 2), "eta_seconds": inc.get("eta_seconds"),
            "hold": {"active": is_held, "since": _iso(inc.get("hold_since")),
                     "released_at": _iso(inc.get("hold_released_at")), "paused_runs": paused},
            "plan": _json(inc.get("plan"), []) or [],
            "flow": flow_of(inc, paused_runs=paused),
            "acknowledged_by": inc.get("acknowledged_by"),
            "acknowledged_at": _iso(inc.get("acknowledged_at")),
            "spend_usd": round(float(inc.get("spend_usd") or 0), 4),
            "agent_run_id": inc.get("agent_run_id"),
            "helper": {"last_seen_at": _iso(seen), "reachable": reachable},
            "report": _json(inc.get("report"), None)}


def current(connection: Any) -> dict[str, Any]:
    return {"incidents": [public(connection, r) for r in open_rows(connection)],
            "hold": work_hold.status(connection), "generated_at": _now().isoformat()}


def listing(connection: Any, *, limit: int = 20, host_id: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE (CAST(:h AS TEXT) IS NULL OR host_id = :h) "
        "ORDER BY opened_at DESC LIMIT :n"), {"h": host_id, "n": max(1, min(int(limit), 200))}
    ).mappings().all()
    return [public(connection, dict(r)) for r in rows]


# ── the machine ─────────────────────────────────────────────────────────────────────────────

def on_level(connection: Any, *, host_id: str, level: str, previous: str,
             sample: Mapping[str, Any], forecast: Any, thresholds: Mapping[str, float],
             floor: bool = False) -> dict[str, Any]:
    """The watch's verdict for one host → open / escalate / progress / resolve."""
    from . import storage_cleanup

    inc = current_for_host(connection, host_id)
    free_now = _free_pct(sample)
    project_id = default_project(connection)
    out: dict[str, Any] = {"action": "none", "incident_id": (inc or {}).get("id")}
    if inc is None:
        if level == "ok":
            return out
        cause_key, cause_text = explain_cause(sample, forecast, thresholds)
        inc_id = make_id("si")
        severity = "critical" if level == "critical" else "warn"
        connection.execute(text("""
            INSERT INTO storage_incidents (id, host_id, host_label, status, severity, opened_at,
                   cause_key, cause_text, free_pct_start, free_pct_now, target_pct, project_id)
            VALUES (:id, :h, :hl, :s, :sev, now(), :ck, :ct, :fs, :fn, :t, :p)"""),
            {"id": inc_id, "h": host_id, "hl": sample.get("host_label"), "s": severity,
             "sev": severity, "ck": cause_key, "ct": cause_text, "fs": free_now, "fn": free_now,
             "t": thresholds.get("target_free_pct", 25.0), "p": project_id})
        _emit(connection, project_id, "storage.pressure", inc_id,
              {"incident_id": inc_id, "host_id": host_id, "level": level, "cause_key": cause_key,
               "free_pct": free_now, "floor": floor})
        _emit(connection, project_id, "storage.incident.opened", inc_id,
              {"incident_id": inc_id, "host_id": host_id, "severity": severity,
               "cause_key": cause_key, "cause_text": cause_text})
        if severity == "critical":
            _hold(connection, inc_id, host_id, project_id)
        inc = _row(connection, inc_id)
        plan = storage_cleanup.build_plan(connection, inc, sample, max_tier=3 if severity == "critical" else 1)
        _update(connection, inc_id, plan=plan)
        inc = _row(connection, inc_id)
        _update(connection, inc_id, flow=flow_of(inc))
        if severity == "critical":
            _admit_agent(connection, inc)
        out.update({"action": "opened", "incident_id": inc_id, "severity": severity})
        return out

    inc_id = inc["id"]
    # The cause stays the one the incident opened (or escalated) with: the rule a person
    # ticks, and the report's "learned", are filed under it (review of PR #300).
    changes: dict[str, Any] = {"free_pct_now": free_now,
                               "host_label": sample.get("host_label") or inc.get("host_label")}
    if level == "critical" and inc.get("severity") != "critical":
        # Escalate: the open incident is reused (one per host).
        cause_key, cause_text = explain_cause(sample, forecast, thresholds)
        changes.update({"severity": "critical", "status": "critical", "cause_key": cause_key,
                        "cause_text": cause_text})
        _update(connection, inc_id, **changes)
        _hold(connection, inc_id, host_id, project_id)
        inc = _row(connection, inc_id)
        _update(connection, inc_id, plan=storage_cleanup.build_plan(connection, inc, sample, max_tier=3,
                                                                    keep=_json(inc.get("plan"), []) or []))
        _emit(connection, project_id, "storage.incident.escalated", inc_id,
              {"incident_id": inc_id, "host_id": host_id, "cause_key": cause_key})
        inc = _row(connection, inc_id)
        _update(connection, inc_id, flow=flow_of(inc))
        _admit_agent(connection, inc)
        out.update({"action": "escalated"})
        return out
    if level == "warn" and inc.get("severity") == "critical":
        changes["severity"] = "warn"
        changes["status"] = "asking" if inc.get("status") == "asking" else "warn"
    if level == "ok":
        _update(connection, inc_id, **changes)
        resolve(connection, inc_id, reason="free space is back above target")
        out.update({"action": "resolved"})
        return out
    # Still warn/critical: progress. The target reached (free ≥ target) releases the hold even
    # before the exit band confirms twice: paused work should not wait for the second reading.
    target = _f(inc.get("target_pct")) or thresholds.get("target_free_pct", 25.0)
    if free_now is not None and free_now >= target and inc.get("hold_since") and not inc.get("hold_released_at"):
        changes["status"] = "resuming"
        changes["hold_released_at"] = _now()
        _update(connection, inc_id, **changes)
        work_hold.release(connection, actor="agent:infra", reason="free space is back above target",
                          project_id=project_id)
        storage_cleanup.skip_remaining(connection, inc_id, reason="target reached")
        out.update({"action": "resuming"})
    else:
        _update(connection, inc_id, **changes)
        out.update({"action": "progress"})
    inc = _row(connection, inc_id)
    _update(connection, inc_id, flow=flow_of(inc))
    return out


def _hold(connection: Any, inc_id: str, host_id: str, project_id: Optional[str]) -> None:
    work_hold.set_hold(connection, incident_id=inc_id, host_id=host_id, actor="agent:infra",
                       project_id=project_id)
    connection.execute(text(
        "UPDATE storage_incidents SET hold_since = COALESCE(hold_since, now()), "
        "hold_released_at = NULL, paused_runs = :n, updated_at = now() WHERE id = :id"),
        {"id": inc_id, "n": work_hold.paused_count(connection)})


def _admit_agent(connection: Any, inc: Mapping[str, Any]) -> None:
    """The Infra Agent reasons about this incident (explain, order, ask, learn) — never on the
    critical path of the deterministic plan. A failure here is logged, never raised."""
    try:
        from . import infra_agent

        run_id = infra_agent.admit_for_incident(connection, inc)
        if run_id:
            _update(connection, inc["id"], agent_run_id=run_id)
    except Exception:  # noqa: BLE001 — the executor never waits on the model
        _log.info("infra agent not admitted for %s", inc["id"], exc_info=True)


def expire_hold(connection: Any, inc: Mapping[str, Any], *, max_seconds: int) -> dict[str, Any]:
    """The hold reached its maximum (LLD review): paused work is released, the incident stays open
    (its card still asks), the report notes it and an alert event is raised."""
    project_id = inc.get("project_id") or default_project(connection)
    out = work_hold.release(connection, actor="boss",
                            reason=f"the work hold reached its maximum of {max_seconds // 60} minutes",
                            project_id=project_id)
    connection.execute(text(
        "UPDATE storage_incidents SET hold_released_at = now(), report = COALESCE(report, '{}'::jsonb) "
        "|| jsonb_build_object('hold_expired', true, 'hold_expired_at', CAST(:t AS text), "
        "'hold_max_seconds', CAST(:m AS integer)), updated_at = now() WHERE id = :id"),
        {"t": _now().isoformat(), "m": int(max_seconds), "id": inc["id"]})
    _emit(connection, project_id, "infra.hold.expired", inc["id"],
          {"incident_id": inc["id"], "host_id": inc["host_id"], "max_seconds": int(max_seconds),
           "resumed_jobs": out.get("resumed_jobs", 0), "status": inc.get("status"),
           "alert": "storage is still below target; work resumed; the incident still needs you"})
    refresh_flow(connection, inc["id"])
    return out


def set_status(connection: Any, incident_id: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    inc = _row(connection, incident_id)
    if inc is None or inc["status"] == "resolved":
        return
    _update(connection, incident_id, status=status)
    if status == "asking":
        _emit(connection, inc.get("project_id"), "storage.incident.asking", incident_id,
              {"incident_id": incident_id, "host_id": inc["host_id"]})
    inc = _row(connection, incident_id)
    _update(connection, incident_id, flow=flow_of(inc))


def add_freed(connection: Any, incident_id: str, gb: float) -> None:
    connection.execute(text(
        "UPDATE storage_incidents SET freed_gb = freed_gb + :g, updated_at = now() WHERE id = :id"),
        {"g": max(0.0, float(gb)), "id": incident_id})


def add_spend(connection: Any, incident_id: str, usd: float) -> None:
    connection.execute(text(
        "UPDATE storage_incidents SET spend_usd = spend_usd + :u, updated_at = now() WHERE id = :id"),
        {"u": max(0.0, float(usd)), "id": incident_id})


def refresh_flow(connection: Any, incident_id: str) -> None:
    inc = _row(connection, incident_id)
    if inc is not None:
        _update(connection, incident_id, flow=flow_of(inc))


def acknowledge(connection: Any, incident_id: str, actor: str) -> Optional[dict[str, Any]]:
    inc = _row(connection, incident_id, lock=True)
    if inc is None:
        return None
    if not inc.get("acknowledged_by"):
        _update(connection, incident_id, acknowledged_by=actor[:200], acknowledged_at=_now())
    return public(connection, _row(connection, incident_id) or inc)


def resolve(connection: Any, incident_id: str, *, reason: str = "") -> Optional[dict[str, Any]]:
    """Release the hold, write the report, learn. Idempotent."""
    inc = _row(connection, incident_id, lock=True)
    if inc is None:
        return None
    if inc["status"] == "resolved":
        return public(connection, inc)
    project_id = inc.get("project_id") or default_project(connection)
    hold = work_hold.state(connection, fresh=True)
    if hold.get("active") and hold.get("incident_id") == incident_id:
        work_hold.release(connection, actor="agent:infra", reason=reason or "incident resolved",
                          project_id=project_id)
    from . import storage_cleanup

    storage_cleanup.skip_remaining(connection, incident_id, reason="incident resolved")
    _update(connection, incident_id, status="resolved", resolved_at=_now(),
            hold_released_at=inc.get("hold_released_at") or (_now() if inc.get("hold_since") else None))
    inc = _row(connection, incident_id) or inc
    learned = learn(connection, inc)
    report = build_report(connection, inc, learned=learned)
    _update(connection, incident_id, report=report)
    inc = _row(connection, incident_id) or inc
    _update(connection, incident_id, flow=flow_of(inc))
    _emit(connection, project_id, "storage.incident.resolved", incident_id,
          {"incident_id": incident_id, "host_id": inc["host_id"], "freed_gb": float(inc.get("freed_gb") or 0),
           "cause_key": inc.get("cause_key"), "reason": reason[:200]})
    try:
        from . import infra_agent

        infra_agent.on_resolved(connection, inc, report)
    except Exception:  # noqa: BLE001
        _log.debug("infra agent not told of the resolution", exc_info=True)
    return public(connection, _row(connection, incident_id) or inc)


# ── learning ────────────────────────────────────────────────────────────────────────────────

RULE_FOR_CAUSE: dict[str, tuple[str, dict[str, Any]]] = {
    "backups.no_retention": ("backups", storage_rules.PROPOSED_BACKUP_RULE),
    "images.no_retention": ("images", {"keep_versions": 2}),
    "docker.vm_unreclaimed": ("docker_trim", {"enabled": True}),
    "logs.uncapped": ("logs", {"max_size_mb": 50, "max_file": 3}),
    "pg.temp_spill": ("telemetry", {"keep_days": 30}),
}


def times_seen(connection: Any, cause_key: str, *, days: int = LEARN_WINDOW_DAYS) -> int:
    return int(connection.execute(text(
        "SELECT count(*) FROM storage_incidents WHERE cause_key = :c AND opened_at >= :since"),
        {"c": cause_key, "since": _now() - timedelta(days=days)}).scalar() or 0)


def duplicates_shipped_default(kind: str, rule: Optional[Mapping[str, Any]]) -> bool:
    """True when every proposed field already matches the rule that ships enabled.

    A backup rule has no shipped default, so it is never a duplicate. A partial telemetry
    rule (`keep_days` only) still matches. An empty proposal does not.
    """
    shipped = storage_rules.DEFAULT_RULES.get(str(kind))
    if shipped is None:
        return False
    proposed = dict(rule or {})
    if not proposed:
        return False
    return all(key in shipped and shipped[key] == value for key, value in proposed.items())


def learn(connection: Any, inc: Mapping[str, Any]) -> dict[str, Any]:
    """A cause seen LEARN_AFTER times in LEARN_WINDOW_DAYS, with no enabled rule for it yet →
    a change-consent decision carrying the concrete rule (`storage_rule`). A proposal that
    only repeats a shipped default is not opened; the default is already in force."""
    cause_key = str(inc.get("cause_key") or "")
    out: dict[str, Any] = {"rule_added": None, "rule_proposed": None}
    if cause_key not in RULE_FOR_CAUSE:
        return out
    kind, rule = RULE_FOR_CAUSE[cause_key]
    existing = storage_rules.rule_for(connection, kind, str(inc["host_id"]))
    if existing is not None and existing.get("source") in ("learned", "operator"):
        out["rule_added"] = existing
        return out
    if duplicates_shipped_default(kind, rule):
        if existing is not None and existing.get("enabled", True):
            out["rule_added"] = existing
        return out
    if times_seen(connection, cause_key) < LEARN_AFTER:
        return out
    from .decision_actions import OPEN_STATUSES

    pending = connection.execute(text(
        "SELECT id FROM decisions WHERE status = ANY(:open) "
        "AND recommendation->>'action' = 'storage_rule' AND recommendation->>'kind' = :k LIMIT 1"),
        {"k": kind, "open": sorted(OPEN_STATUSES)}).scalar()
    if pending:
        out["rule_proposed"] = {"decision_id": pending, "kind": kind, "rule": rule}
        return out
    decision_id = propose_rule(connection, inc, kind=kind, rule=rule,
                               why=f"{cause_key} was seen {times_seen(connection, cause_key)} "
                                   f"times in {LEARN_WINDOW_DAYS} days")
    if not decision_id:
        if existing is not None and existing.get("enabled", True):
            out["rule_added"] = existing
        return out
    out["rule_proposed"] = {"decision_id": decision_id, "kind": kind, "rule": rule}
    return out


def propose_rule(connection: Any, inc: Mapping[str, Any], *, kind: str, rule: Mapping[str, Any],
                 why: str, actor: str = "agent:infra") -> Optional[str]:
    """Open a `storage_rule` card. None when the proposal copies a shipped default."""
    if duplicates_shipped_default(kind, rule):
        return None
    from .core import create_blocking_decision

    project_id = inc.get("project_id") or default_project(connection)
    label = storage_rules.rule_label(kind, rule)
    decision_id = create_blocking_decision(
        connection, project_id, None,
        f"Make this a permanent rule: {storage_rules.LABELS.get(kind, kind)} — {label}?",
        {"action": "storage_rule", "kind": kind, "rule": dict(rule), "host_id": inc.get("host_id"),
         "incident_id": inc["id"], "cause_key": inc.get("cause_key"), "why": why,
         "ui": "StorageRuleProposal"},
        {"estimated_cost_usd": 0.0}, why, risk_tier=2, default_action="hold")
    _emit(connection, project_id, "storage.rule.proposed", inc["id"],
          {"incident_id": inc["id"], "host_id": inc.get("host_id"), "kind": kind,
           "rule": dict(rule), "decision_id": decision_id})
    return decision_id


def on_rule_decision(connection: Any, decision: Mapping[str, Any], *, verdict: str,
                     actor: str) -> Optional[dict[str, Any]]:
    """The decisions service's hook for `storage_rule`: approve → retention_rules row (source
    'learned') + a KG lesson; reject → nothing (re-proposed on the next repeat)."""
    rec = _json(decision.get("recommendation"), {}) or {}
    if verdict != "approve":
        return None
    kind = str(rec.get("kind") or "")
    if kind not in storage_rules.KINDS:
        return None
    row = storage_rules.upsert(connection, kind=kind, rule=dict(rec.get("rule") or {}),
                               host_id=rec.get("host_id"), source="learned", actor=actor,
                               decision_id=str(decision.get("id") or ""))
    _lesson(connection, decision.get("project_id"), kind, row, rec)
    return row


def _lesson(connection: Any, project_id: Any, kind: str, rule: Mapping[str, Any],
            rec: Mapping[str, Any]) -> None:
    try:
        from .knowledge import learning_writeback

        learning_writeback.record(
            connection, project_id=str(project_id or default_project(connection) or ""),
            learning_kind="rule",
            title=f"Storage: {storage_rules.LABELS.get(kind, kind)} retention",
            statement=f"On this install, {kind} is kept as {rule.get('label')}; the cause "
                      f"{rec.get('cause_key')} recurred and the operator approved the rule.",
            detail={"kind": kind, "rule": dict(rule.get("rule") or {}), "cause_key": rec.get("cause_key")},
            about={"areas": ["infra.storage"], "rules": [f"retention:{kind}"], "symbols": []},
            evidence=[{"type": "metric", "ref": f"storage_incident:{rec.get('incident_id')}",
                       "excerpt": str(rec.get("why") or "")[:200]}],
            confidence=0.8)
    except Exception:  # noqa: BLE001 — a lesson is a bonus, never a failure
        _log.debug("KG lesson not written", exc_info=True)


# ── the report ──────────────────────────────────────────────────────────────────────────────

def paused_work(connection: Any, inc: Mapping[str, Any], *, org_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Jobs this incident paused (stamped with its id), with how long each waited."""
    if org_id is not None:
        if not org_id:
            return []
        rows = connection.execute(text("""
            SELECT j.id, j.kind, j.payload, j.last_error, j.created_at, j.updated_at FROM jobs j
            JOIN projects p ON p.id = j.project_id
            JOIN workspaces w ON w.id = p.workspace_id
             WHERE j.last_error->>'incident_id' = :i AND w.org_id = :org
             ORDER BY j.created_at LIMIT 500"""),
            {"i": inc["id"], "org": str(org_id)}).mappings().all()
    else:
        rows = connection.execute(text("""
            SELECT id, kind, payload, last_error, created_at, updated_at FROM jobs
             WHERE last_error->>'incident_id' = :i ORDER BY created_at LIMIT 500"""),
            {"i": inc["id"]}).mappings().all()
    out = []
    for r in rows:
        err = _json(r.get("last_error"), {}) or {}
        payload = _json(r.get("payload"), {}) or {}
        paused_at = err.get("paused_at")
        resumed_at = err.get("resumed_at")
        waited = None
        try:
            if paused_at and resumed_at:
                waited = int((datetime.fromisoformat(resumed_at) - datetime.fromisoformat(paused_at)).total_seconds())
        except ValueError:
            waited = None
        out.append({"job_id": r["id"], "kind": r["kind"], "run_id": payload.get("run_id"),
                    "title": payload.get("title") or r["kind"], "paused_at": paused_at,
                    "resumed_at": resumed_at, "waited_seconds": waited})
    return out


def build_report(connection: Any, inc: Mapping[str, Any], *, learned: Optional[Mapping[str, Any]] = None,
                 org_id: Optional[str] = None) -> dict[str, Any]:
    plan = _json(inc.get("plan"), []) or []
    paused = paused_work(connection, inc, org_id=org_id)
    waited = max([p["waited_seconds"] or 0 for p in paused] or [0])
    opened, resolved_at = inc.get("opened_at"), inc.get("resolved_at") or _now()
    duration = int((resolved_at - opened).total_seconds()) if opened else 0
    removed = []
    for s in plan:
        if s.get("state") != "done" or not (s.get("freed_gb") or s.get("items")):
            continue
        by = ("person" if s.get("approved_by") or s.get("tier") == 3
              else "rule" if s.get("tier") == 2 else "rebuildable")
        removed.append({"type": s.get("label") or s.get("tool"), "detail": s.get("detail") or
                        f"{s.get('items') or 0} items", "by": by,
                        "gb": round(float(s.get("freed_gb") or 0), 2), "receipt_id": s.get("receipt_id")})
    freed = float(inc.get("freed_gb") or 0)
    chips = [c for c in [inc.get("cause_key"), "two readings agreed", "dry run first"] if c]
    summary = (f"{inc.get('cause_text') or 'Storage ran low.'} BOSS paused {len(paused)} "
               f"{'run' if len(paused) == 1 else 'runs'} at a checkpoint, freed {freed:.1f} GB "
               f"safest first and resumed everything; nothing was lost or run twice.")
    learned = learned or {}
    return {"summary": summary, "freed_gb": round(freed, 2), "paused_runs": len(paused),
            "waited_seconds_max": waited, "lost_or_repeated": 0,
            "cost_usd": round(float(inc.get("spend_usd") or 0), 4), "duration_seconds": duration,
            "cause": {"key": inc.get("cause_key"), "text": inc.get("cause_text") or "", "chips": chips},
            "removed": removed,
            "learned": {"rule_added": learned.get("rule_added"),
                        "rule_proposed": learned.get("rule_proposed")},
            "timeline": flow_of(inc, paused_runs=len(paused))}


def report_markdown(connection: Any, inc: Mapping[str, Any], *, org_id: Optional[str] = None) -> str:
    if org_id is not None:
        stored = _json(inc.get("report"), None)
        learned_data = stored.get("learned") if isinstance(stored, Mapping) else None
        rep = build_report(connection, inc, learned=learned_data, org_id=org_id)
    else:
        rep = _json(inc.get("report"), None) or build_report(connection, inc)
    lines = [f"# Storage incident {inc['id']} on {inc.get('host_label') or inc['host_id']}", "",
             rep["summary"], "",
             f"- Freed: {rep['freed_gb']} GB", f"- Work paused: {rep['paused_runs']} "
             f"(longest wait {rep['waited_seconds_max']} s)", "- Lost or repeated: 0",
             f"- Cost: ${rep['cost_usd']:.2f}", f"- Duration: {rep['duration_seconds']} s", "",
             "## Flow", ""]
    for n in rep["timeline"]:
        lines.append(f"- {n['label']} — {n['state']}" + (f" at {n['at']}" if n.get("at") else "")
                     + (f": {n['detail']}" if n.get("detail") else ""))
    lines += ["", "## Cause", "", rep["cause"]["text"] or "—", "", "## What was removed", ""]
    for r in rep["removed"] or []:
        lines.append(f"- {r['type']}: {r['detail']} · {r['gb']} GB · by {r['by']}"
                     + (f" · receipt {r['receipt_id']}" if r.get("receipt_id") else ""))
    if not rep["removed"]:
        lines.append("- nothing needed removing")
    learned = rep.get("learned") or {}
    lines += ["", "## Learned", ""]
    if learned.get("rule_added"):
        lines.append(f"- rule in force: {learned['rule_added'].get('label')}")
    if learned.get("rule_proposed"):
        lines.append(f"- rule proposed (decision {learned['rule_proposed']['decision_id']}): "
                     f"{learned['rule_proposed']['kind']} {learned['rule_proposed']['rule']}")
    if not learned.get("rule_added") and not learned.get("rule_proposed"):
        lines.append("- nothing new")
    return "\n".join(lines) + "\n"


def detail(connection: Any, incident_id: str) -> Optional[dict[str, Any]]:
    inc = _row(connection, incident_id)
    if inc is None:
        return None
    actions = connection.execute(text(
        "SELECT * FROM storage_actions WHERE incident_id = :i ORDER BY created_at"),
        {"i": incident_id}).mappings().all()
    receipt_ids = [a["receipt_id"] for a in actions if a.get("receipt_id")]
    receipts = []
    if receipt_ids:
        receipts = [dict(r) for r in connection.execute(text(
            "SELECT id, dedupe_key, effect_kind, status, created_at FROM receipts WHERE id = ANY(:ids) "
            "ORDER BY created_at"), {"ids": receipt_ids}).mappings().all()]
    decisions = [dict(r) for r in connection.execute(text(
        "SELECT id, status, question, created_at, recommendation->>'action' AS action FROM decisions "
        "WHERE recommendation->>'incident_id' = :i ORDER BY created_at"), {"i": incident_id}).mappings().all()]
    from .decision_actions import kind_of

    return {"incident": public(connection, inc),
            "actions": [_action_public(a) for a in actions],
            "receipts": [{**r, "created_at": _iso(r.get("created_at"))} for r in receipts],
            "paused_work": paused_work(connection, inc),
            "decisions": [{"id": d["id"], "kind": kind_of({"recommendation": {"action": d["action"]}}),
                           "status": d["status"], "question": d["question"],
                           "created_at": _iso(d["created_at"])} for d in decisions]}


def _action_public(a: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": a["id"], "tool": a["tool"], "tier": int(a["tier"]), "where": a.get("where_run") or "host",
            "dry_run": bool(a.get("dry_run")), "status": a["status"], "items": a.get("items"),
            "bytes": int(a["bytes"]) if a.get("bytes") is not None else None,
            "plan_hash": a["plan_hash"], "receipt_id": a.get("receipt_id"),
            "decision_id": a.get("decision_id"), "actor": a["actor"],
            "started_at": _iso(a.get("started_at")), "ended_at": _iso(a.get("ended_at")),
            "result": _json(a.get("result"), {}) or {}}


def _emit(connection: Any, project_id: Any, event_type: str, entity_id: str, data: dict[str, Any]) -> None:
    try:
        pid = project_id or default_project(connection)
        if pid:
            emit_event(connection, str(pid), event_type, "agent:infra", "storage_incident",
                       entity_id, data)
    except Exception:  # noqa: BLE001
        _log.debug("event %s not emitted", event_type, exc_info=True)


# ── multi-org Option A scoping (BOSSAPP-1415) ────────────────────────────────────────────────

def is_separated(connection: Any) -> bool:
    """True when at least one active instance admin exists (separated install)."""
    try:
        from ..identity import instance_admin

        with connection.begin_nested() if hasattr(connection, "begin_nested") else _NullCtx():
            return bool(instance_admin.layer(connection, "").separated)
    except Exception:  # noqa: BLE001 — unit tests without identity tables default to unseparated
        return False


def is_install_operator(connection: Any, principal_id: Optional[str]) -> bool:
    """True when `principal_id` may mutate or view full install-wide storage details.

    Instance admins always qualify. Before any instance admin exists, a single-org install
    keeps owner/admin operator access; on a multi-org install an org-scoped user is NOT an
    install operator even before `identity_instance_admins` is populated.
    """
    try:
        from ..identity import instance_admin

        with connection.begin_nested() if hasattr(connection, "begin_nested") else _NullCtx():
            found = instance_admin.layer(connection, str(principal_id or ""))
            if found.is_admin:
                return True
            if found.separated:
                return False
            org_count = int(connection.execute(text("SELECT count(*) FROM orgs")).scalar() or 0)
            return org_count <= 1
    except Exception:  # noqa: BLE001 — unit tests without identity tables default to single-org
        return True


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def project_org_id(connection: Any, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    try:
        with connection.begin_nested() if hasattr(connection, "begin_nested") else _NullCtx():
            val = connection.execute(
                text("SELECT w.org_id FROM projects p "
                     "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
                {"p": str(project_id)},
            ).scalar()
            return str(val) if val else None
    except Exception:  # noqa: BLE001
        return None


def incident_org_id(connection: Any, inc: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not inc:
        return None
    return project_org_id(connection, inc.get("project_id"))


def incident_visible_to_org(connection: Any, inc: Optional[Mapping[str, Any]],
                            org_id: Optional[str]) -> bool:
    """An org caller sees its own org's incidents and a redacted install-wide summary (never
    another org's incidents)."""
    if not inc or not org_id:
        return False
    inc_org = incident_org_id(connection, inc)
    return inc_org is None or inc_org == org_id


def org_paused_count(connection: Any, org_id: Optional[str],
                     *, incident_id: Optional[str] = None) -> int:
    if not org_id:
        return 0
    try:
        with connection.begin_nested() if hasattr(connection, "begin_nested") else _NullCtx():
            sql = (
                "SELECT count(*) FROM jobs j "
                "JOIN projects p ON p.id = j.project_id "
                "JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE j.status IN ('queued', 'failed_retryable') "
                "  AND j.last_error->>'code' = :code "
                "  AND w.org_id = :org"
            )
            params: dict[str, Any] = {"code": work_hold.REASON, "org": str(org_id)}
            if incident_id:
                sql += " AND j.last_error->>'incident_id' = :inc"
                params["inc"] = str(incident_id)
            return int(connection.execute(text(sql), params).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def org_scoped_summary_incident(connection: Any, inc: Mapping[str, Any],
                                org_id: str) -> dict[str, Any]:
    """Read-only org-scoped view of an incident (redacted if install-wide, org-paused count)."""
    inc_org = incident_org_id(connection, inc)
    hold = work_hold.state(connection)
    is_held = bool(hold.get("active") and hold.get("incident_id") == inc["id"])
    paused = org_paused_count(connection, org_id, incident_id=inc["id"])
    if inc_org == org_id:
        out = public(connection, inc)
        out["hold"] = {**out["hold"], "paused_runs": paused}
        out["flow"] = flow_of(inc, paused_runs=paused)
        if isinstance(out.get("report"), dict):
            out["report"] = build_report(
                connection, inc, learned=out["report"].get("learned"), org_id=org_id
            )
        out["org_scoped"] = True
        out["read_only"] = True
        return out
    return {
        "id": inc["id"],
        "host_id": inc["host_id"],
        "host_label": inc.get("host_label") or inc["host_id"],
        "status": inc["status"],
        "severity": inc.get("severity") or "warn",
        "opened_at": _iso(inc.get("opened_at")),
        "resolved_at": _iso(inc.get("resolved_at")),
        "cause_key": inc.get("cause_key"),
        "cause_text": inc.get("cause_text") or "",
        "free_pct_start": _f(inc.get("free_pct_start")),
        "free_pct_now": _f(inc.get("free_pct_now")),
        "target_pct": _f(inc.get("target_pct")) or 25.0,
        "freed_gb": round(float(inc.get("freed_gb") or 0), 2),
        "eta_seconds": inc.get("eta_seconds"),
        "hold": {
            "active": is_held,
            "since": _iso(inc.get("hold_since")),
            "released_at": _iso(inc.get("hold_released_at")),
            "paused_runs": paused,
        },
        "plan": [],
        "flow": [],
        "acknowledged_by": None,
        "acknowledged_at": None,
        "spend_usd": 0.0,
        "agent_run_id": None,
        "helper": {"last_seen_at": None, "reachable": False},
        "report": None,
        "org_scoped": True,
        "read_only": True,
    }


def scoped_hold_status(connection: Any, org_id: Optional[str], *, is_operator: bool) -> dict[str, Any]:
    st = work_hold.status(connection)
    if is_operator:
        return st
    if not org_id:
        return {
            **st,
            "incident_id": None,
            "paused_runs": 0,
            "actor": "",
            "org_scoped": True,
            "read_only": True,
        }
    inc_id = st.get("incident_id")
    if inc_id:
        inc = get(connection, str(inc_id))
        if not incident_visible_to_org(connection, inc, org_id):
            st = {**st, "incident_id": None}
    return {
        **st,
        "paused_runs": org_paused_count(connection, org_id),
        "actor": "",
        "org_scoped": True,
        "read_only": True,
    }


def scoped_current(connection: Any, org_id: Optional[str], *, is_operator: bool) -> dict[str, Any]:
    if is_operator:
        return current(connection)
    if not org_id:
        return {
            "incidents": [],
            "hold": scoped_hold_status(connection, None, is_operator=False),
            "generated_at": _now().isoformat(),
            "org_scoped": True,
            "read_only": True,
        }
    visible = [
        org_scoped_summary_incident(connection, r, org_id)
        for r in open_rows(connection)
        if incident_visible_to_org(connection, r, org_id)
    ]
    return {
        "incidents": visible,
        "hold": scoped_hold_status(connection, org_id, is_operator=False),
        "generated_at": _now().isoformat(),
        "org_scoped": True,
        "read_only": True,
    }


def scoped_listing(connection: Any, org_id: Optional[str], *, is_operator: bool,
                   limit: int = 20, host_id: Optional[str] = None) -> list[dict[str, Any]]:
    if is_operator:
        return listing(connection, limit=limit, host_id=host_id)
    if not org_id:
        return []
    rows = connection.execute(text(
        "SELECT * FROM storage_incidents WHERE (CAST(:h AS TEXT) IS NULL OR host_id = :h) "
        "ORDER BY opened_at DESC LIMIT :n"), {"h": host_id, "n": max(1, min(int(limit), 200))}
    ).mappings().all()
    return [
        org_scoped_summary_incident(connection, dict(r), org_id)
        for r in rows
        if incident_visible_to_org(connection, r, org_id)
    ]


def scoped_detail(connection: Any, incident_id: str, org_id: Optional[str],
                  *, is_operator: bool) -> Optional[dict[str, Any]]:
    if is_operator:
        return detail(connection, incident_id)
    if not org_id:
        return None
    inc = _row(connection, incident_id)
    if inc is None or not incident_visible_to_org(connection, inc, org_id):
        return None
    inc_org = incident_org_id(connection, inc)
    if inc_org is None:
        return {
            "incident": org_scoped_summary_incident(connection, inc, org_id),
            "actions": [],
            "receipts": [],
            "paused_work": paused_work(connection, inc, org_id=org_id),
            "decisions": [],
            "org_scoped": True,
            "read_only": True,
        }
    base = detail(connection, incident_id)
    if base is None:
        return None
    base["incident"] = org_scoped_summary_incident(connection, inc, org_id)
    base["paused_work"] = paused_work(connection, inc, org_id=org_id)
    base["decisions"] = _org_decisions_for_incident(connection, incident_id, org_id)
    base["org_scoped"] = True
    base["read_only"] = True
    return base


def _job_in_org(connection: Any, job_id: str, org_id: str) -> bool:
    try:
        return bool(connection.execute(text(
            "SELECT 1 FROM jobs j JOIN projects p ON p.id = j.project_id "
            "JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE j.id = :j AND w.org_id = :org LIMIT 1"),
            {"j": job_id, "org": org_id}).first())
    except Exception:  # noqa: BLE001
        return False


def _org_decisions_for_incident(connection: Any, incident_id: str, org_id: str) -> list[dict[str, Any]]:
    from .decision_actions import kind_of

    rows = connection.execute(text(
        "SELECT d.id, d.status, d.question, d.created_at, d.recommendation->>'action' AS action "
        "FROM decisions d JOIN projects p ON p.id = d.project_id "
        "JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE d.recommendation->>'incident_id' = :i AND w.org_id = :org ORDER BY d.created_at"),
        {"i": incident_id, "org": org_id}).mappings().all()
    return [
        {"id": d["id"], "kind": kind_of({"recommendation": {"action": d["action"]}}),
         "status": d["status"], "question": d["question"], "created_at": _iso(d["created_at"])}
        for d in rows
    ]


def scoped_summary(connection: Any, org_id: Optional[str], *, is_operator: bool,
                   host_id: Optional[str] = None) -> dict[str, Any]:
    from . import storage_watch

    out = storage_watch.summary(connection, host_id)
    if is_operator:
        return out
    if not org_id:
        out["open_incident_id"] = None
        out["last_cleanup"] = None
    else:
        open_id = out.get("open_incident_id")
        if open_id:
            inc = get(connection, str(open_id))
            if not incident_visible_to_org(connection, inc, org_id):
                out["open_incident_id"] = None
        last = out.get("last_cleanup")
        if isinstance(last, dict) and last.get("incident_id"):
            inc = get(connection, str(last["incident_id"]))
            if not incident_visible_to_org(connection, inc, org_id):
                out["last_cleanup"] = None
    out["rules"] = [
        {**r, "created_by": None, "decision_id": None}
        for r in (out.get("rules") or [])
    ]
    out["org_scoped"] = True
    out["read_only"] = True
    return out


__all__ = ["AGENT_KEY", "FLOW_KEYS", "LEARN_AFTER", "MAX_LABEL_WORDS", "RULE_FOR_CAUSE", "STATUSES",
           "acknowledge", "add_freed", "add_spend", "build_report", "current", "current_for_host",
           "default_project", "detail", "explain_cause", "flow_of", "get", "incident_org_id",
           "incident_visible_to_org", "is_install_operator", "is_separated", "last_resolved",
           "learn", "level_of", "listing", "on_level", "on_rule_decision", "open_rows",
           "org_paused_count", "org_scoped_summary_incident", "paused_work", "project_org_id",
           "propose_rule", "public", "refresh_flow", "report_markdown", "resolve",
           "scoped_current", "scoped_detail", "scoped_hold_status", "scoped_listing",
           "scoped_summary", "set_status", "short", "times_seen"]
