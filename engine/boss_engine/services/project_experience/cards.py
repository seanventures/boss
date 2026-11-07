"""What a Projects-list card shows beyond §1's fields (screen 02), batched for every project:

  uptime_pct           the health ring's number: probe samples that answered, last 7 days
  onboarding_progress  [done, total] of the onboarding checklist (the teal ring while it moves)
  merged, building     "3 merged · 1 building"
  lines                the two status lines: ("staging healthy", "3 merged · 1 building"),
                       ("mapping code", "1,284 nodes so far"), ("not deployed yet", "idle")
  crew                 the crew's faces, one per job (Plans · Builds · Tests · Deploys), from the
                       project's real roster; `state: working` on the ones acting right now
  live_agents          the agents acting right now (an open act on a live run or build): the
                       Running tile's faces
  week_cap_usd         the project's weekly budget cap (daily × 7, or monthly × 7 / 30)

One query per fact for all projects; nothing here calls out of BOSS.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import avatar_of
from .health import env_label

#: Who does the work, one face per job in the order a card shows them; each job lists the role
#: keys real rosters use (an org's "pm" / "software_engineer" as well as the seeded names).
CREW_SLOTS = (("pm", "product_manager", "product_owner", "planner"),
              ("software_engineer", "builder", "engineer", "backend_engineer",
               "frontend_engineer", "developer"),
              ("qa", "qa_engineer", "tester"),
              ("sre", "site_reliability_engineer", "devops_engineer", "deployment_manager"))
CREW_KEYS = tuple(k for slot in CREW_SLOTS for k in slot)
CHECK_TOTAL = 7


def _week_cap(daily: Any, monthly: Any) -> float | None:
    daily, monthly = int(daily or 0), int(monthly or 0)
    micros = daily * 7 if daily else (monthly * 7 // 30 if monthly else 0)
    return round(micros / 1_000_000, 2) if micros else None


def extras(connection: Connection, project_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(p) for p in project_ids))
    if not ids:
        return {}
    p = {"ids": ids}
    uptime = {str(r["project_id"]): r for r in connection.execute(text(
        "SELECT t.project_id, count(*) FILTER (WHERE s.ok) AS ok, count(*) AS n FROM "
        "deployment_health_samples s JOIN deployment_targets t ON t.id = s.deployment_target_id "
        "WHERE t.project_id = ANY(:ids) AND t.status = 'active' AND s.checked_at > now() - "
        "interval '7 days' GROUP BY t.project_id"), p).mappings()}
    deployed = {str(r["project_id"]): r for r in connection.execute(text(
        "SELECT DISTINCT ON (t.project_id) t.project_id, t.id, t.environment, "
        "(SELECT ok FROM deployment_health_samples h WHERE h.deployment_target_id = t.id "
        " ORDER BY checked_at DESC LIMIT 1) AS ok FROM deployment_targets t "
        "WHERE t.project_id = ANY(:ids) AND t.status = 'active' AND EXISTS (SELECT 1 FROM "
        "stack_deployments s WHERE s.target_id = t.id AND s.status = 'healthy') "
        "ORDER BY t.project_id, (lower(t.environment) IN ('staging', 'stage')) DESC"),
        p).mappings()}
    # THE SAME HEALTH WORD as the card's dot, the header and the Health tab (health.state_of).
    from .health import target_states

    states = target_states(connection, [str(r["id"]) for r in deployed.values()])
    onboarding = {str(r["project_id"]): r for r in connection.execute(text(
        "SELECT DISTINCT ON (project_id) project_id, state, checklist FROM project_onboarding_runs "
        "WHERE project_id = ANY(:ids) ORDER BY project_id, started_at DESC"), p).mappings()}
    merged = {str(r[0]): int(r[1]) for r in connection.execute(text(
        "SELECT project_id, count(DISTINCT finalize_ctx->'merge'->>'sha') FROM code_task_runs "
        "WHERE project_id = ANY(:ids) AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
        "GROUP BY project_id"), p).all()}
    building = {str(r[0]): int(r[1]) for r in connection.execute(text(
        "SELECT project_id, count(*) FROM code_task_runs WHERE project_id = ANY(:ids) "
        "AND status IN ('running', 'queued') GROUP BY project_id"), p).all()}
    graph = {str(r[0]): int(r[1] or 0) for r in connection.execute(text(
        "SELECT project_id, sum(nodes) FROM kg_repo_index WHERE project_id = ANY(:ids) "
        "GROUP BY project_id"), p).all()}
    caps = {str(r["scope_id"]): _week_cap(r["daily_cap_usd_micros"], r["monthly_cap_usd_micros"])
            for r in connection.execute(text(
                "SELECT DISTINCT ON (scope_id) scope_id, daily_cap_usd_micros, "
                "monthly_cap_usd_micros FROM budget_policies WHERE scope_type = 'project' AND "
                "scope_id = ANY(:ids) ORDER BY scope_id, created_at DESC"), p).mappings()}
    crews: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT project_id, role_key, display_name, spec FROM roles WHERE project_id = ANY(:ids) "
            "AND role_key = ANY(:keys) AND COALESCE(lifecycle, 'active') = 'active'"),
            {"ids": ids, "keys": list(CREW_KEYS)}).mappings():
        spec = r["spec"] if isinstance(r["spec"], dict) else {}
        persona = spec.get("persona") if isinstance(spec.get("persona"), dict) else {}
        crews.setdefault(str(r["project_id"]), []).append({
            "name": persona.get("callsign") or r["display_name"], "role_key": r["role_key"],
            "role": r["display_name"], "avatar": avatar_of(spec, str(r["role_key"]))})
    # Who is acting RIGHT NOW: an open act (no end) on a run or build that is still live.
    working: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (a.project_id, a.role_key) a.project_id, a.role_key, "
            "ro.display_name, ro.spec FROM agent_acts a JOIN roles ro ON ro.project_id = "
            "a.project_id AND ro.role_key = a.role_key WHERE a.project_id = ANY(:ids) "
            "AND a.ended_at IS NULL AND a.status = 'active' AND a.role_key IS NOT NULL AND ("
            " EXISTS (SELECT 1 FROM workflow_runs w WHERE w.id = a.workflow_run_id AND w.status IN "
            "  ('queued', 'validating', 'running')) OR "
            " EXISTS (SELECT 1 FROM code_task_runs c WHERE c.id = a.code_task_run_id AND c.status "
            "  IN ('running', 'queued'))) "
            "ORDER BY a.project_id, a.role_key, a.started_at DESC"), p).mappings():
        spec = r["spec"] if isinstance(r["spec"], dict) else {}
        persona = spec.get("persona") if isinstance(spec.get("persona"), dict) else {}
        working.setdefault(str(r["project_id"]), []).append({
            "name": persona.get("callsign") or r["display_name"], "role_key": r["role_key"],
            "role": r["display_name"], "avatar": avatar_of(spec, str(r["role_key"])),
            "state": "working"})
    out: dict[str, dict[str, Any]] = {}
    for pid in ids:
        up = uptime.get(pid)
        uptime_pct = round(100.0 * int(up["ok"]) / int(up["n"]), 1) if up and up["n"] else None
        onb = onboarding.get(pid)
        progress = None
        if onb:
            checklist = onb["checklist"] if isinstance(onb["checklist"], dict) else \
                json.loads(onb["checklist"] or "{}")
            done = sum(1 for i in checklist.values() if i.get("state") in ("done", "skipped"))
            progress = [done, max(CHECK_TOTAL, len(checklist))]
        m, b = merged.get(pid, 0), building.get(pid, 0)
        work = " · ".join(x for x in (f"{m} merged" if m else "",
                                      f"{b} building" if b else "") if x) or "idle"
        if m and not b:
            work = f"{m} merged · idle"
        active_onboarding = bool(onb and onb["state"] == "running")
        dep = deployed.get(pid)
        if active_onboarding:
            nodes = graph.get(pid, 0)
            lines = ["mapping code", f"{nodes:,} nodes so far" if nodes else "reading the repo"]
            tone = "now"
        elif dep:
            state = (states.get(str(dep["id"])) or {}).get("health", "unknown")
            word = {"unknown": "no signal yet"}.get(state, state)
            lines = [f"{env_label(dep['environment']).lower()} {word}", work]
            tone = {"healthy": "done", "down": "bad", "degraded": "you"}.get(state)
        else:
            lines = ["not deployed yet", work]
            tone = None
        now_on = {f["role_key"] for f in working.get(pid, [])}
        have = {f["role_key"]: f for f in crews.get(pid, [])}
        crew = []
        for slot in CREW_SLOTS:
            # The job's face: the one acting now, else the first the roster has.
            key = next((k for k in slot if k in have and k in now_on), None) or \
                next((k for k in slot if k in have), None)
            if key:
                crew.append({**have[key], "state": "working" if key in now_on else "idle"})
        out[pid] = {"uptime_pct": uptime_pct, "onboarding_progress": progress, "merged": m,
                    "building": b, "lines": lines, "line_tone": tone,
                    "crew": crew, "live_agents": working.get(pid, [])[:4],
                    "week_cap_usd": caps.get(pid)}
    return out


__all__ = ["CREW_KEYS", "CREW_SLOTS", "extras"]
