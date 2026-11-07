"""What a Projects-list card shows beyond §1's fields (screen 02), batched for every project:

  uptime_pct           the health ring's number: the card's environment's 24 h uptime
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
from typing import Any, Iterable, Mapping

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


def live_work(connection: Connection, project_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """THE ONE "WHAT MOVES NOW" (02-05, 02-11/14): per project, the pieces of work that are live —
    a product run in a live state (common.LIVE_RUN), or a build running or queued that no live
    run already counts — and, for each, the agent on it: the one with an open act, else the last
    one that acted on it. The card's Running count, its Working pill, its "N building" line and
    the Running tile's faces all read this, so a count is never drawn without its faces.
    {project_id: {"count": n, "agents": [face, …]}}"""
    from .common import LIVE_RUN

    ids = list(dict.fromkeys(str(p) for p in project_ids))
    out: dict[str, dict[str, Any]] = {pid: {"count": 0, "agents": []} for pid in ids}
    if not ids:
        return out
    rows = connection.execute(text(
        "WITH lw AS ("
        " SELECT r.project_id, r.id AS run_id, NULL::text AS build_id FROM workflow_runs r "
        " JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        " WHERE r.project_id = ANY(:ids) AND d.namespace = 'product' "
        " AND COALESCE(r.mode, 'normal') <> 'test' AND r.status = ANY(:live) "
        " UNION ALL "
        " SELECT c.project_id, NULL::text, c.id FROM code_task_runs c WHERE c.project_id = ANY(:ids) "
        " AND c.status IN ('running', 'queued') AND NOT EXISTS (SELECT 1 FROM workflow_runs w "
        " WHERE w.id = c.finalize_ctx->>'workflow_run_id' AND w.status = ANY(:live))) "
        "SELECT lw.project_id, lw.run_id, lw.build_id, who.role_key, who.open, ro.display_name, "
        "ro.spec FROM lw LEFT JOIN LATERAL (SELECT a.role_key, a.ended_at IS NULL AS open "
        " FROM agent_acts a WHERE a.project_id = lw.project_id AND a.role_key IS NOT NULL AND ("
        " (lw.run_id IS NOT NULL AND (a.workflow_run_id = lw.run_id OR a.code_task_run_id IN ("
        "   SELECT c2.id FROM code_task_runs c2 WHERE c2.finalize_ctx->>'workflow_run_id' = lw.run_id)))"
        " OR (lw.build_id IS NOT NULL AND a.code_task_run_id = lw.build_id)) "
        " ORDER BY (a.ended_at IS NULL) DESC, a.started_at DESC LIMIT 1) who ON true "
        "LEFT JOIN roles ro ON ro.project_id = lw.project_id AND ro.role_key = who.role_key"),
        {"ids": ids, "live": list(LIVE_RUN)}).mappings().all()
    bare = [str(r["run_id"]) for r in rows if not r["role_key"] and r["run_id"]]
    on_step = _step_agents(connection, bare)
    roster: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in rows:
        pid = str(r["project_id"])
        slot = out.setdefault(pid, {"count": 0, "agents": []})
        slot["count"] += 1
        key = r["role_key"] or (on_step.get(str(r["run_id"])) if r["run_id"] else
                                "software_engineer")
        if not key or any(a["role_key"] == key for a in slot["agents"]):
            continue
        role = r if r["role_key"] else roster.setdefault((pid, key), connection.execute(text(
            "SELECT display_name, spec FROM roles WHERE project_id = :p AND role_key = :k"),
            {"p": pid, "k": key}).mappings().first() or {})
        spec = role.get("spec") if isinstance(role.get("spec"), dict) else {}
        persona = spec.get("persona") if isinstance(spec.get("persona"), dict) else {}
        slot["agents"].append({
            "name": persona.get("callsign") or role.get("display_name") or str(key).replace(
                "_", " ").title(), "role_key": key, "role": role.get("display_name"),
            "avatar": avatar_of(spec, str(key)), "state": "working"})
    return out


def _step_agents(connection: Connection, run_ids: list[str]) -> dict[str, str]:
    """A live run nobody has acted on yet: the agent of the step it is on (command_center.
    step_agent — the same "who is on this step" the Work row's faces read)."""
    if not run_ids:
        return {}
    from ..command_center import step_agent

    out: dict[str, str] = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (s.workflow_run_id) s.workflow_run_id, s.step_id, s.status, "
            "s.output, v.spec FROM workflow_step_runs s JOIN workflow_runs w ON w.id = "
            "s.workflow_run_id JOIN workflow_versions v ON v.id = w.workflow_version_id "
            "WHERE s.workflow_run_id = ANY(:ids) AND s.status IN ('running', 'leased', 'ready', "
            "'waiting') ORDER BY s.workflow_run_id, s.created_at DESC"),
            {"ids": run_ids}).mappings():
        spec = r["spec"] if isinstance(r["spec"], dict) else json.loads(r["spec"] or "{}")
        step = next((x for x in spec.get("steps") or [] if isinstance(x, dict)
                     and str(x.get("id")) == str(r["step_id"])), {})
        output = r["output"] if isinstance(r["output"], dict) else json.loads(r["output"] or "{}")
        who = step_agent(step, {"status": r["status"], "output": output}, {})
        if who.get("role_key") and who.get("how") != "person":
            out[str(r["workflow_run_id"])] = str(who["role_key"])
    return out


def extras(connection: Connection, project_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(p) for p in project_ids))
    if not ids:
        return {}
    p = {"ids": ids}
    every: dict[str, list[Any]] = {}
    for r in connection.execute(text(
            "SELECT t.project_id, t.id, t.environment FROM deployment_targets t "
            "WHERE t.project_id = ANY(:ids) AND t.status = 'active' AND EXISTS (SELECT 1 FROM "
            "stack_deployments s WHERE s.target_id = t.id AND s.status = 'healthy') "
            "ORDER BY t.project_id, (lower(t.environment) IN ('staging', 'stage')) DESC, t.id"),
            p).mappings():
        every.setdefault(str(r["project_id"]), []).append(r)
    # THE SAME HEALTH WORD as the card's dot, the header and the Health tab (health.state_of),
    # and ONE environment per card (02-13/14): the one whose health the card says — the worst
    # (health.worst, the summary's own rule), staging first among equals — so the ring's number
    # (its 24 h uptime, the Health tab's) and the "staging healthy" line are about the same app.
    from .health import target_states

    states = target_states(connection, [str(r["id"]) for rs in every.values() for r in rs])
    rank = {"down": 0, "degraded": 1, "healthy": 2}
    deployed = {pid: min(rs, key=lambda r: rank.get(
        (states.get(str(r["id"])) or {}).get("health", "unknown"), 3)) for pid, rs in every.items()}
    onboarding = {str(r["project_id"]): r for r in connection.execute(text(
        "SELECT DISTINCT ON (project_id) project_id, state, checklist FROM project_onboarding_runs "
        "WHERE project_id = ANY(:ids) ORDER BY project_id, started_at DESC"), p).mappings()}
    merged = {str(r[0]): int(r[1]) for r in connection.execute(text(
        "SELECT project_id, count(DISTINCT finalize_ctx->'merge'->>'sha') FROM code_task_runs "
        "WHERE project_id = ANY(:ids) AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
        "GROUP BY project_id"), p).all()}
    moving = live_work(connection, ids)
    building = {pid: int(v["count"]) for pid, v in moving.items()}
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
    # Who is acting RIGHT NOW: the agent on each live piece of work (live_work).
    working = {pid: v["agents"] for pid, v in moving.items()}
    out: dict[str, dict[str, Any]] = {}
    for pid in ids:
        dep = deployed.get(pid)
        # The ring's number: the card's environment's 24 h uptime (target_states → the Health
        # tab's "99.79% up"), never a 7-day mix of every environment beside a 24 h verdict.
        uptime_pct = (states.get(str(dep["id"])) or {}).get("uptime_pct") if dep else None
        uptime_pct = round(float(uptime_pct), 2) if uptime_pct is not None else None
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


__all__ = ["CREW_KEYS", "CREW_SLOTS", "extras", "live_work"]
