"""Projects list (contract §1) and the project Overview (contract §2).

Stage: where the project is on its journey (onboarding → ready → backlog → working → staging →
production). A stage is REACHED when BOSS has the record of it (an onboarding run finished, a
proposed backlog, a piece of work, a healthy non-production deploy, a healthy production
deploy); the CURRENT stage is `working` while work is moving and otherwise the furthest reached.
Needs you is decision_actions.waiting_on_operator — the one reader every surface uses — plus the
onboarding agent's "only you can do this" items.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import captain as captain_service
from . import demo_logins as demo
from . import onboarding as onboarding_service
from . import work as work_service
from .common import (LIVE_RUN, STAGE_LABELS, STAGES, STATUS_LABELS, agents, avatar_of, face, href,
                     initials, iso, pr_number, project_href, repos, require_project, short_day,
                     words)
from .health import environments as health_environments
from .health import rollup

PRODUCTION = ("production", "prod")


# ── the journey ─────────────────────────────────────────────────────────────────────────────

def reached(*, onboarded: bool, backlog: bool, worked: bool, staging: bool, production: bool,
            onboarding_running: bool) -> dict[str, bool]:
    return {"onboarding": True, "ready": onboarded and not onboarding_running,
            "backlog": backlog, "working": worked, "staging": staging, "production": production}


def stage_of(got: Mapping[str, bool], *, active: bool) -> str:
    if active:
        return "working"
    furthest = "onboarding"
    for key in STAGES:
        if got.get(key):
            furthest = key
    return furthest


def journey(project_id: str, got: Mapping[str, bool], stage: str, *, needs_you: int = 0,
            subs: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    out = []
    for key in STAGES:
        if key == stage:
            state = "waiting" if needs_you and key == "working" else "now"
        elif got.get(key):
            state = "done"
        else:
            state = "todo"
        out.append({"key": key, "label": STAGE_LABELS[key], "state": state,
                    "sub": (subs or {}).get(key) or "",
                    "href": project_href(project_id, tab=_TAB_OF.get(key, "overview"),
                                         drawer=f"stage:{key}")})
    return out


_TAB_OF = {"onboarding": "overview", "ready": "overview", "backlog": "roadmap",
           "working": "work", "staging": "deployments", "production": "deployments"}


# ── needs you ───────────────────────────────────────────────────────────────────────────────

def _decision_kind(decision: Mapping[str, Any]) -> str:
    from ..decision_actions import kind_of

    kind = kind_of(decision)
    rec = decision.get("recommendation") if isinstance(decision.get("recommendation"), dict) else {}
    cap = str(rec.get("capability") or "")
    if kind == "pull_request" or cap in ("release_approval", "wait_for_ci"):
        return "merge"
    if kind in ("deploy_gate", "change_set") or cap in ("deploy", "redeploy"):
        return "deploy"
    if kind == "architecture_review" or cap in ("spec_review", "meeting", "research",
                                                "architecture"):
        return "plan"
    if kind == "refusal":
        return "setup"
    return "question"


_ACTION_WORDS = {"merge": "Review PR", "deploy": "Approve deploy", "plan": "Review plan",
                 "setup": "Fix setup", "question": "Answer"}


def needs_you(connection: Connection, project_id: str, work_items: list[dict[str, Any]],
              onboarding: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    from ..decision_actions import waiting_on_operator

    runs = {w["id"]: w for w in work_items if w.get("source") == "workflow_run"}
    by_decision = {d: w for w in work_items for d in w.get("decision_ids") or []}
    out = []
    for d in waiting_on_operator(connection, project_id):
        rec = d.get("recommendation") if isinstance(d.get("recommendation"), dict) else {}
        kind = _decision_kind(d)
        run = runs.get(str(rec.get("workflow_run_id") or "")) or by_decision.get(str(d["id"]))
        links = (run or {}).get("links") or {}
        pr_url = rec.get("pull_request_url") or (links.get("pr") or {}).get("external_url")
        number = pr_number(pr_url) if pr_url else None
        env = str(rec.get("environment") or "").strip()
        if kind == "merge" and number:
            label = f"Merge PR #{number}"
        elif kind == "deploy":
            label = f"Deploy to {env}" if env else "Approve a deploy"
        elif kind == "plan":
            label = "Approve the plan"
        else:
            label = words(d.get("question") or "Answer a question", 4)
        mini = [{"label": s["name"], "state": s["state"]} for s in (run or {}).get("stages") or []
                if s["state"] != "skipped"]
        out.append({"id": d["id"], "kind": kind, "label": label,
                    "detail": str(d.get("question") or ""),
                    "work_id": (run or {}).get("id"), "work_label": (run or {}).get("label"),
                    "mini": mini, "risk_tier": d.get("risk_tier"),
                    "created_at": iso(d.get("created_at")),
                    "external_url": pr_url or None,
                    "action": {"label": _ACTION_WORDS[kind], "decision_id": d["id"],
                               "href": href(project_id, area="decisions", decision=d["id"])}})
    for item in (onboarding or {}).get("only_you") or []:
        action = item.get("action") or {}
        out.append({"id": f"onb:{item['key']}", "kind": "setup", "label": item["label"],
                    "detail": item.get("detail") or "", "mini": [], "work_id": None,
                    "action": {"label": action.get("label") or "Open",
                               "href": action.get("href") or project_href(project_id,
                                                                          drawer="onboarding"),
                               "decision_id": None}})
    return out


# ── tiles ───────────────────────────────────────────────────────────────────────────────────

#: A finished day's spend, per project, for this long: a past day's money barely moves, and the
#: sparkline would otherwise cost 8 ledger reads on every Overview load.
_DAY_TTL_S = 600.0
_DAY_CACHE: dict[tuple[str, str, str], tuple[float, float]] = {}


def _money(connection: Connection, project_id: str, start: datetime, end: Optional[datetime],
           now: datetime) -> float:
    """ledger_spend's money for [start, end), in dollars; a window that ended is cached."""
    import time as _time

    from ..included_spend import ledger_spend

    closed = end is not None and end <= now
    key = (project_id, start.isoformat(), end.isoformat() if end else "")
    hit = _DAY_CACHE.get(key) if closed else None
    if hit and _time.monotonic() - hit[1] < _DAY_TTL_S:
        return hit[0]
    value = ledger_spend(connection, project_id, start, end,
                         with_calls=False)["money_usd_micros"] / 1_000_000
    if closed:
        if len(_DAY_CACHE) > 5000:
            _DAY_CACHE.clear()
        _DAY_CACHE[key] = (value, _time.monotonic())
    return value


def _cost_week(connection: Connection, project_id: str, now: datetime) -> dict[str, Any]:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = [day0 - timedelta(days=i) for i in range(6, -1, -1)]
    series = []
    for i, start in enumerate(days):
        end = days[i + 1] if i + 1 < len(days) else None
        series.append(round(_money(connection, project_id, start, end, now), 2))
    week = round(sum(series), 2)
    before = _money(connection, project_id, days[0] - timedelta(days=7), days[0], now)
    delta = round((week - before) * 100.0 / before) if before > 0 else None
    policy = connection.execute(text(
        "SELECT daily_cap_usd_micros, monthly_cap_usd_micros FROM budget_policies "
        "WHERE scope_type = 'project' AND scope_id = :p ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    cap = None
    if policy:
        daily = int(policy["daily_cap_usd_micros"] or 0)
        monthly = int(policy["monthly_cap_usd_micros"] or 0)
        micros = daily * 7 if daily else (monthly * 7 // 30 if monthly else 0)
        cap = round(micros / 1_000_000, 2) if micros else None
    return {"usd": week, "delta_pct": delta, "cap_usd": cap, "series": series,
            "href": href(project_id, area="budget")}


def tiles(project_id: str, items: list[dict[str, Any]], envs: list[dict[str, Any]],
          cost: dict[str, Any]) -> dict[str, Any]:
    by_state = work_service.counts(items)
    open_count = sum(by_state[k] for k in ("running", "needs_you", "planned"))
    running = [w for w in items if w["state"] == "running"]
    current = None
    if running:
        w = running[0]
        live = [s for s in w["stages"] if s["state"] != "skipped"]
        done = sum(1 for s in live if s["state"] == "done")
        current = {"id": w["id"], "label": w["label"], "progress": [done, len(live)],
                   "agent": (w.get("agents") or [None])[0], "href": w["href"]}
    staging = next((e for e in envs if str(e["key"]).lower() in ("staging", "stage")), None) or \
        next((e for e in envs if str(e["key"]).lower() not in PRODUCTION), None)
    return {
        "open_work": {"count": open_count, "by_state": by_state,
                      "href": project_href(project_id, tab="work")},
        "running": {"count": len(running), "current": current,
                    "href": project_href(project_id, tab="work") + "&state=running"},
        "staging": ({"health": staging["health"], "uptime_pct": staging["uptime_pct"],
                     "p95_ms": staging["p95_ms"], "series": staging["series"]["latency"],
                     "label": staging["label"], "url": staging.get("url"),
                     "href": project_href(project_id, tab="health")}
                    if staging else {"health": "unknown", "uptime_pct": None, "p95_ms": None,
                                     "series": [], "label": "Staging", "url": None,
                                     "href": project_href(project_id, tab="deployments")}),
        "cost_week": cost,
    }


# ── what's next and the week ────────────────────────────────────────────────────────────────

def next_items(connection: Connection, project_id: str, onboarding: Mapping[str, Any],
               limit: int = 4) -> list[dict[str, Any]]:
    recs = connection.execute(text(
        "SELECT work_item_id, title, why, kind, detector FROM ambient_recommendations "
        "WHERE project_id = :p AND status = 'open' ORDER BY score DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    out = [{"id": r["work_item_id"], "label": words(r["title"], 5), "detail": r["why"] or r["title"],
            "source": "roadmap", "state": "next",
            "href": project_href(project_id, tab="roadmap", drawer=f"rec:{r['work_item_id']}"),
            "accept": {"href": f"/v1/ambient/roadmap/{r['work_item_id']}/accept", "method": "POST",
                       "body": {"as": "ticket", "project_id": project_id}}} for r in recs]
    for c in (onboarding.get("backlog") or [])[: max(0, limit - len(out))]:
        source = c.get("source") if c.get("source") in ("roadmap", "ticket", "bug", "ci") \
            else "roadmap"
        out.append({"id": c["id"], "label": c["label"], "detail": c.get("detail"),
                    "source": source, "state": "proposed", "external_url": c.get("external_url"),
                    "href": c.get("href") or project_href(project_id, tab="roadmap"),
                    "accept": {"href": f"/v1/projects/{project_id}/work", "method": "POST",
                               "body": {"kind": c["kind"] if c.get("kind") in
                                        ("feature", "bug", "ticket") else "ticket",
                                        "text": c.get("text") or c["label"],
                                        "title": c["label"], "source_ref": c.get("ref")}}})
    return out[:limit]


def week(connection: Connection, project_id: str, now: datetime, limit: int = 12
         ) -> list[dict[str, Any]]:
    since = now - timedelta(days=7)
    out: list[dict[str, Any]] = []
    for r in connection.execute(text(
            "SELECT s.id, s.finished_at, s.commit_sha, t.environment FROM stack_deployments s "
            "JOIN deployment_targets t ON t.id = s.target_id WHERE s.project_id = :p "
            "AND s.status = 'healthy' AND s.finished_at >= :s ORDER BY s.finished_at DESC LIMIT 6"),
            {"p": project_id, "s": since}).mappings():
        out.append({"at": iso(r["finished_at"]), "kind": "deploy",
                    "label": words(f"Deployed to {r['environment']}"),
                    "href": href(project_id, area="infra", view="deploys", deploy=r["id"])})
    for r in connection.execute(text(
            "SELECT id, pull_request_url, pull_request_number, created_at, updated_at, status "
            "FROM code_task_runs WHERE project_id = :p AND COALESCE(pull_request_url, '') <> '' "
            "AND updated_at >= :s ORDER BY updated_at DESC LIMIT 6"),
            {"p": project_id, "s": since}).mappings():
        n = r["pull_request_number"] or pr_number(r["pull_request_url"])
        out.append({"at": iso(r["updated_at"]), "kind": "pr", "label": f"PR #{n} opened" if n
                    else "PR opened", "external_url": r["pull_request_url"],
                    "href": href(project_id, area="code_tasks", view="apps", build=r["id"])})
    for r in connection.execute(text(
            "SELECT id, approved_at, recommendation->>'pull_request_url' AS url FROM decisions "
            "WHERE project_id = :p AND status IN ('approved', 'executed') AND approved_at >= :s "
            "AND recommendation->>'action' = 'review_pull_request' ORDER BY approved_at DESC "
            "LIMIT 6"), {"p": project_id, "s": since}).mappings():
        n = pr_number(r["url"])
        out.append({"at": iso(r["approved_at"]), "kind": "merge",
                    "label": f"PR #{n} merged" if n else "PR merged", "external_url": r["url"],
                    "href": href(project_id, area="decisions", decision=r["id"])})
    for r in connection.execute(text(
            "SELECT id, title, first_seen FROM incidents WHERE project_id = :p AND first_seen >= :s "
            "ORDER BY first_seen DESC LIMIT 4"), {"p": project_id, "s": since}).mappings():
        out.append({"at": iso(r["first_seen"]), "kind": "incident", "label": words(r["title"]),
                    "href": project_href(project_id, tab="health")})
    for r in connection.execute(text(
            "SELECT id, COALESCE(title, directive) AS title, updated_at FROM meeting_runs "
            "WHERE project_id = :p AND status = 'completed' AND updated_at >= :s "
            "ORDER BY updated_at DESC LIMIT 4"), {"p": project_id, "s": since}).mappings():
        out.append({"at": iso(r["updated_at"]), "kind": "idea", "label": words(r["title"]),
                    "href": href(project_id, area="meetings", meeting=r["id"])})
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    return out[:limit]


# ── the overview ────────────────────────────────────────────────────────────────────────────

def _facts(connection: Connection, project_id: str, items: list[dict[str, Any]],
           envs: list[dict[str, Any]], onboarding: Mapping[str, Any]) -> dict[str, Any]:
    healthy_non_prod = any(str(e["key"]).lower() not in PRODUCTION and e.get("deployed_at")
                           for e in envs)
    healthy_prod = any(str(e["key"]).lower() in PRODUCTION and e.get("deployed_at") for e in envs)
    has_recs = bool(connection.execute(text(
        "SELECT 1 FROM ambient_recommendations WHERE project_id = :p LIMIT 1"),
        {"p": project_id}).first())
    got = reached(onboarded=onboarding.get("state") in ("ready", "needs_you") or bool(items),
                  backlog=bool(onboarding.get("backlog")) or has_recs or bool(items),
                  worked=any(w["source"] != "roadmap" for w in items),
                  staging=healthy_non_prod, production=healthy_prod,
                  onboarding_running=onboarding.get("state") == "running")
    active = any(w["state"] in ("running", "needs_you") for w in items)
    return {"got": got, "active": active}


def view(connection: Connection, project_id: str, *, now: Optional[datetime] = None
         ) -> dict[str, Any]:
    from .deployments import live_url

    now = now or datetime.now(timezone.utc)
    project = require_project(connection, project_id)
    roster = agents(connection, project_id)
    items = work_service.everything(connection, project_id, now=now)
    envs = health_environments(connection, project_id, now=now)
    onboarding = onboarding_service.view(connection, project_id)
    facts = _facts(connection, project_id, items, envs, onboarding)
    stage = stage_of(facts["got"], active=facts["active"])
    waiting = needs_you(connection, project_id, items, onboarding)
    the_repos = repos(connection, project_id)
    url = live_url(envs)
    logins = demo.listing(connection, project_id)
    try_env = next((e for e in logins["environments"] if str(e["key"]).lower() in
                    ("staging", "stage")), None) or next(iter(logins["environments"]), None)
    subs = {"onboarding": short_day(project["created_at"]) or "",
            "working": f"{sum(1 for w in items if w['state'] == 'running')} running"
            if facts["active"] else ""}
    return {
        "project": {"id": project["id"], "name": project["name"], "initials":
                    initials(project["name"]), "description": project["description"],
                    "stage": stage, "repos": the_repos, "live_url": url,
                    "health": rollup(envs), "status_label": STATUS_LABELS[stage],
                    "archived": project["archived_at"] is not None},
        "journey": journey(project_id, facts["got"], stage,
                           needs_you=len(waiting), subs=subs),
        "needs_you": waiting,
        "tiles": tiles(project_id, items, envs, _cost_week(connection, project_id, now)),
        "captain": captain_service.view(connection, project_id, roster=roster,
                                        with_last_said=False),
        "recent_work": items[:5],
        "next": next_items(connection, project_id, onboarding),
        "try_it": {"url": url, "env": try_env["key"] if try_env else None,
                   "demo_logins": try_env["logins"] if try_env else [],
                   "users_from": logins["source"]},
        "onboarding": {"state": onboarding["state"], "stages": onboarding["stages"],
                       "href": onboarding.get("href") or project_href(project_id,
                                                                      drawer="onboarding")},
        "week": week(connection, project_id, now),
    }


# ── the projects list (§1), batched across projects ─────────────────────────────────────────

def _card_cost(connection: Connection, project_id: str, now: datetime) -> dict[str, Any]:
    """A card's week of spend (the Overview's cost tile, the same numbers): `cost_week` dollars
    and its 7-day `series`. Past days come from the day cache; a card never fails on money."""
    try:
        with connection.begin_nested():
            cost = _cost_week(connection, project_id, now)
    except Exception:  # noqa: BLE001 — the ledger unreadable: the card shows no cost, not an error
        return {"cost_week": None, "series": []}
    return {"cost_week": cost["usd"], "series": cost["series"]}


def summary(connection: Connection, project_ids: Iterable[str], *,
            now: Optional[datetime] = None) -> list[dict[str, Any]]:
    from ..decision_actions import count_waiting_on_operator

    ids = list(dict.fromkeys(str(p) for p in project_ids))
    if not ids:
        return []
    now = now or datetime.now(timezone.utc)
    projects = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT id, name, created_at, archived_at FROM projects WHERE id = ANY(:ids)"),
        {"ids": ids}).mappings()}
    primary = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (project_id) project_id, full_name FROM github_repository_bindings "
            "WHERE project_id = ANY(:ids) AND status = 'active' "
            "ORDER BY project_id, (role = 'primary') DESC, created_at"), {"ids": ids}).mappings():
        primary[str(r["project_id"])] = r["full_name"]
    runs = {str(r["project_id"]): dict(r) for r in connection.execute(text(
        "SELECT r.project_id, count(*) FILTER (WHERE r.status = ANY(:live)) AS live, "
        "count(*) AS total, max(r.updated_at) AS updated FROM workflow_runs r "
        "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        "WHERE r.project_id = ANY(:ids) AND d.namespace = 'product' "
        "AND COALESCE(r.mode, 'normal') <> 'test' GROUP BY r.project_id"),
        {"ids": ids, "live": list(LIVE_RUN)}).mappings()}
    builds = {str(r["project_id"]): dict(r) for r in connection.execute(text(
        "SELECT project_id, count(*) FILTER (WHERE status IN ('running', 'queued')) AS live, "
        "count(*) AS total, max(updated_at) AS updated FROM code_task_runs "
        "WHERE project_id = ANY(:ids) GROUP BY project_id"), {"ids": ids}).mappings()}
    targets: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT t.project_id, t.id, t.environment, t.domains, t.public_url, "
            "(SELECT ok FROM deployment_health_samples s WHERE s.deployment_target_id = t.id "
            " ORDER BY checked_at DESC LIMIT 1) AS ok, "
            "(SELECT open_url FROM stack_deployments s WHERE s.target_id = t.id "
            " AND s.status = 'healthy' ORDER BY created_at DESC LIMIT 1) AS open_url, "
            "EXISTS (SELECT 1 FROM stack_deployments s WHERE s.target_id = t.id "
            " AND s.status = 'healthy') AS deployed "
            "FROM deployment_targets t WHERE t.project_id = ANY(:ids) AND t.status = 'active'"),
            {"ids": ids}).mappings():
        targets.setdefault(str(r["project_id"]), []).append(dict(r))
    onboarding = {str(r["project_id"]): dict(r) for r in connection.execute(text(
        "SELECT DISTINCT ON (project_id) project_id, state, jsonb_array_length(backlog) AS backlog "
        "FROM project_onboarding_runs WHERE project_id = ANY(:ids) "
        "ORDER BY project_id, started_at DESC"), {"ids": ids}).mappings()}
    recs = {str(r[0]) for r in connection.execute(text(
        "SELECT DISTINCT project_id FROM ambient_recommendations WHERE project_id = ANY(:ids)"),
        {"ids": ids}).all()}
    captains = {str(r["project_id"]): str(r["role_key"]) for r in connection.execute(text(
        "SELECT project_id, role_key FROM project_captains WHERE project_id = ANY(:ids)"),
        {"ids": ids}).mappings()}
    roles: dict[str, dict[str, dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT project_id, role_key, display_name, spec, agent_id, lifecycle FROM roles "
            "WHERE project_id = ANY(:ids) AND (role_key = ANY(:keys) OR role_key LIKE '%captain%')"),
            {"ids": ids, "keys": sorted(set(captains.values()) | {captain_service.DEFAULT_ROLE})}
    ).mappings():
        spec = r["spec"] if isinstance(r["spec"], dict) else {}
        persona = spec.get("persona") if isinstance(spec.get("persona"), dict) else {}
        roles.setdefault(str(r["project_id"]), {})[str(r["role_key"])] = {
            "agent_id": r["agent_id"] or r["role_key"], "role_key": r["role_key"],
            "name": persona.get("callsign") or r["display_name"], "role": r["display_name"],
            "avatar": avatar_of(spec, str(r["role_key"])), "lifecycle": r["lifecycle"] or "active"}
    out = []
    for pid in ids:
        p = projects.get(pid)
        if p is None:
            continue
        run, build = runs.get(pid, {}), builds.get(pid, {})
        tgs = targets.get(pid, [])
        onb = onboarding.get(pid, {})
        waiting = count_waiting_on_operator(connection, pid, now=now)
        live = int(run.get("live") or 0) + int(build.get("live") or 0)
        worked = int(run.get("total") or 0) + int(build.get("total") or 0) > 0
        non_prod = any(t["deployed"] and str(t["environment"]).lower() not in PRODUCTION
                       for t in tgs)
        prod = any(t["deployed"] and str(t["environment"]).lower() in PRODUCTION for t in tgs)
        got = reached(onboarded=onb.get("state") in ("ready", "needs_you") or worked,
                      backlog=bool(onb.get("backlog")) or pid in recs or worked, worked=worked,
                      staging=non_prod, production=prod,
                      onboarding_running=onb.get("state") == "running")
        stage = stage_of(got, active=bool(live or waiting) and worked)
        health_words = ["healthy" if t["ok"] else "down" for t in tgs
                        if t["deployed"] and t["ok"] is not None]
        health = ("down" if "down" in health_words and "healthy" not in health_words else
                  "degraded" if "down" in health_words else
                  "healthy" if health_words else "unknown")
        ordered = sorted(tgs, key=lambda t: (str(t["environment"]).lower() not in ("staging",
                                                                                  "stage"),
                                             str(t["environment"]).lower() in PRODUCTION))
        url = next((t["open_url"] or t["public_url"] or ((t["domains"] or [None])[0]
                                                         if isinstance(t["domains"], list) else None)
                    for t in ordered if t["deployed"]), None)
        roster = roles.get(pid, {})
        key = captains.get(pid) if captains.get(pid) in roster else captain_service.default_role(roster)
        f = face(roster, key) if key else None
        updated = max([v for v in (run.get("updated"), build.get("updated"), p["created_at"])
                       if v is not None])
        out.append({"id": pid, "name": p["name"], "initials": initials(p["name"]),
                    "repo": primary.get(pid), "stage": stage,
                    "status_label": STATUS_LABELS[stage],
                    "journey": [{"key": j["key"], "label": j["label"], "state": j["state"]}
                                for j in journey(pid, got, stage, needs_you=waiting)],
                    "needs_you": waiting, "running": live, "health": health, "live_url": url,
                    "captain": ({"name": f["name"], "role_key": f["role_key"],
                                 "avatar": f["avatar"], "agent_id": f.get("agent_id")}
                                if f else None),
                    "onboarding": onb.get("state"), "archived": p["archived_at"] is not None,
                    **_card_cost(connection, pid, now),
                    "updated_at": iso(updated), "href": project_href(pid)})
    return out


__all__ = ["journey", "needs_you", "next_items", "reached", "stage_of", "summary", "tiles",
           "view", "week"]
