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
from .common import (LIVE_RUN, web_url, STAGE_LABELS, STAGES, STATUS_LABELS, agents, avatar_of, face, href,
                     initials, iso, pr_number, project_href, repos, require_project, short_day,
                     words)
from .health import environments as health_environments
from .health import rollup

PRODUCTION = ("production", "prod")


# ── the journey ─────────────────────────────────────────────────────────────────────────────

def reached(*, onboarded: bool, backlog: bool, worked: bool, staging: bool, production: bool,
            onboarding_running: bool) -> dict[str, bool]:
    # ONE SOURCE OF TRUTH for "Onboarded" (PX-15): done only when onboarding finished or the
    # project has had work; while a run moves it is the current stage; otherwise not started.
    return {"onboarding": onboarded and not onboarding_running,
            "_onboarding_running": onboarding_running,
            "ready": onboarded and not onboarding_running,
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
            subs: Optional[Mapping[str, str]] = None, details: Optional[Mapping[str, str]] = None,
            prod_waits: bool = False, active: bool = True) -> list[dict[str, Any]]:
    """The six stages. The current one is `now` (teal, pulsing) — work waiting on a person is
    said in its sub and the Needs-you card, not by locking the Working node; Production is
    `waiting` (amber, "needs you") whenever it waits on a person (staging is live, no prod
    host yet). `detail` is the node's tooltip in words."""
    out = []
    for key in STAGES:
        if key == stage == "onboarding" and not got.get("_onboarding_running", True) \
                and not got.get("onboarding"):
            state = "todo"
        elif key == stage and (active or not got.get(key)):
            # `now` pulses: only while something moves (work running or waiting on a person, an
            # onboarding pass). A quiet project's furthest stage is done, not live.
            state = "now"
        elif got.get(key):
            state = "done"
        elif key == "production" and prod_waits:
            state = "waiting"
        else:
            state = "todo"
        not_started = key == "onboarding" and state == "todo"
        out.append({"key": key, "label": STAGE_LABELS[key], "state": state,
                    "sub": "Not started" if not_started else (subs or {}).get(key) or "",
                    "detail": ("Onboarding hasn't run yet: start it and Helm reads the repo"
                               if not_started else (details or {}).get(key) or ""),
                    "href": project_href(project_id, tab=_TAB_OF.get(key, "overview"),
                                         drawer=f"stage:{key}")})
    return out


_TAB_OF = {"onboarding": "overview", "ready": "overview", "backlog": "roadmap",
           "working": "work", "staging": "deployments", "production": "deployments"}


def journey_subs(connection: Connection, project: Mapping[str, Any], items: list[dict[str, Any]],
                 envs: list[dict[str, Any]], onboarding: Mapping[str, Any],
                 waiting: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str], bool]:
    """Each node's few words (screen 04: "Nov 2", "6/6", "7 of 12", "3 merged", "2h ago",
    "needs you") and its tooltip, all from the record; and whether production waits on a person."""
    from .common import age

    project_id = str(project["id"])
    checklist = onboarding.get("checklist") or []
    done = sum(1 for c in checklist if c.get("state") in ("done", "skipped"))
    open_recs, accepted = (int(v or 0) for v in connection.execute(text(
        "SELECT count(*) FILTER (WHERE status = 'open'), count(*) FILTER (WHERE status = "
        "'accepted') FROM ambient_recommendations WHERE project_id = :p"),
        {"p": project_id}).first() or (0, 0))
    proposed = len(onboarding.get("backlog") or []) + open_recs + accepted
    merged = int(connection.execute(text(
        "SELECT count(DISTINCT finalize_ctx->'merge'->>'sha') FROM code_task_runs "
        "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> ''"),
        {"p": project_id}).scalar() or 0)
    running = sum(1 for w in items if w["state"] == "running" and w["source"] != "onboarding")
    people = [n for n in waiting if not str(n.get("id", "")).startswith("onb:")]

    def newest(prod: bool) -> Optional[datetime]:
        at = [e.get("deployed_at") for e in envs if (str(e["key"]).lower() in PRODUCTION) == prod
              and e.get("deployed_at")]
        if not at:
            return None
        return max(datetime.fromisoformat(str(a)) if isinstance(a, str) else a for a in at)

    staged, shipped = newest(False), newest(True)
    prod_waits = staged is not None and shipped is None
    created = short_day(project["created_at"]) or ""
    subs = {"onboarding": created,
            "ready": f"{done}/{len(checklist)}" if checklist else "",
            "backlog": (f"{accepted} of {proposed}" if accepted else f"{proposed} proposed")
            if proposed else "",
            "working": (f"{merged} merged" if merged else f"{running} running" if running
                        else f"{len(people)} need you" if people else ""),
            "staging": f"{age(staged)} ago" if staged else "",
            "production": f"{age(shipped)} ago" if shipped else
            ("needs you" if prod_waits else "Set up")}
    stack = ((onboarding.get("discover") or {}).get("nodes") or [])
    found = ", ".join(str(n.get("sub")) for n in stack if n.get("sub"))[:80]
    working = ", ".join(x for x in (f"{merged} merged" if merged else "",
                                    f"{running} building" if running else "",
                                    f"{len(people)} waiting on you" if people else "") if x)
    details = {
        "onboarding": f"Onboarded {created}" + (f" — {found}" if found else ""),
        "ready": (f"Ready for work: {done} of {len(checklist)} checks" if checklist
                  else "Ready for work"),
        "backlog": (f"Backlog: {proposed} items proposed, you accepted {accepted}" if proposed
                    else "No backlog ranked yet"),
        "working": f"Working: {working}" if working else "No work yet",
        "staging": (f"Staging last deployed {age(staged)} ago" if staged
                    else "Staging deploys after the first merge"),
        "production": ("Production needs a person: pick a host, then approve" if prod_waits
                       else f"Production last deployed {age(shipped)} ago" if shipped
                       else "Production is set up once staging is live"),
    }
    return subs, details, prod_waits


# ── needs you ───────────────────────────────────────────────────────────────────────────────

def _decision_kind(decision: Mapping[str, Any]) -> str:
    from .needs import decision_kind

    return decision_kind(decision)


def needs_you(connection: Connection, project_id: str, work_items: list[dict[str, Any]],
              onboarding: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Needs you in words (needs.cards): labels and mini flows from each decision's data."""
    from .needs import cards

    return cards(connection, project_id, work_items, onboarding)


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
    # ON A SUBSCRIPTION the week costs no money, but the work still has a list price: say so
    # ("On subscription · ≈ $57.55 at list price"), never a bare $0.00 (PX-10).
    included = [0.0] * 7
    for r in connection.execute(text(
            "SELECT date_trunc('day', created_at) AS d, sum(cost_usd_micros) AS m FROM llm_calls "
            "WHERE project_id = :p AND included_in_subscription AND created_at >= :s "
            "GROUP BY 1"), {"p": project_id, "s": days[0]}).mappings():
        at = r["d"] if r["d"].tzinfo else r["d"].replace(tzinfo=timezone.utc)
        i = (at.date() - days[0].date()).days
        if 0 <= i < 7:
            included[i] += round(float(r["m"] or 0) / 1_000_000, 2)
    on_plan = week == 0 and sum(included) > 0
    # 04-15 the delta on a subscription week is the LIST price against last week's list price
    # (the money is $0 both weeks, so the money delta says nothing).
    list_delta = None
    if on_plan:
        row = connection.execute(text(
            "SELECT COALESCE(sum(cost_usd_micros), 0) AS m FROM llm_calls WHERE project_id = :p "
            "AND included_in_subscription AND created_at >= :a AND created_at < :b"),
            {"p": project_id, "a": days[0] - timedelta(days=7), "b": days[0]}).mappings().first()
        list_before = float((row or {}).get("m") or 0) / 1_000_000
        list_delta = (round((sum(included) - list_before) * 100.0 / list_before)
                      if list_before > 0 else None)
    return {"usd": week, "delta_pct": delta, "cap_usd": cap, "series": series,
            # "first week": nothing was spent last week, so there is no % to compare with.
            "first_week": (before <= 0 and week > 0) if not on_plan else list_delta is None,
            "list_delta_pct": list_delta,
            "cap_pct": (round(week * 100.0 / cap) if cap else None),
            "on_subscription": on_plan,
            "list_usd": round(sum(included), 2) if sum(included) else None,
            "list_series": [round(v, 2) for v in included],
            "href": href(project_id, area="budget", tab="limits")}


def tiles(project_id: str, items: list[dict[str, Any]], envs: list[dict[str, Any]],
          cost: dict[str, Any]) -> dict[str, Any]:
    by_state = work_service.counts(items)
    open_count = sum(by_state[k] for k in ("running", "stalled", "needs_you", "planned"))
    running = [w for w in items if w["state"] == "running"]
    # The tile's "now" is the build moving, before the onboarding agent's own pass.
    running.sort(key=lambda w: w["source"] == "onboarding")
    current = None
    if running:
        w = running[0]
        live = [s for s in w["stages"] if s["state"] != "skipped"]
        done = sum(1 for s in live if s["state"] == "done")
        now_stage = next((st for st in live if st["state"] == "now"), None)
        doing = {"plan": "Planning", "design": "Designing", "build": "Building",
                 "review": "Reviewing", "pr": "Opening PR", "ship": "Shipping"}.get(
            str((now_stage or {}).get("key")), "Working")
        current = {"id": w["id"], "label": w["label"], "progress": [done, len(live)],
                   "sub": f"{doing} · {w['age']}" if w.get("age") else doing,
                   "agent": (w.get("agents") or [None])[0], "href": w["href"]}
    staging = next((e for e in envs if str(e["key"]).lower() in ("staging", "stage")), None) or \
        next((e for e in envs if str(e["key"]).lower() not in PRODUCTION), None)
    return {
        "open_work": {"count": open_count, "by_state": by_state,
                      "href": project_href(project_id, tab="work")},
        "running": {"count": len(running), "current": current,
                    "href": project_href(project_id, tab="work") + "&state=running"},
        "staging": ({"health": staging["health"], "uptime_pct": staging["uptime_pct"],
                     "p95_ms": staging["p95_ms"], "p95_source": staging.get("p95_source"),
                     "series": staging["series"]["latency"],
                     "label": staging["label"], "url": staging.get("url"),
                     "href": project_href(project_id, tab="health")}
                    if staging else {"health": "unknown", "uptime_pct": None, "p95_ms": None,
                                     "series": [], "label": "Staging", "url": None,
                                     "href": project_href(project_id, tab="deployments")}),
        "cost_week": cost,
    }


# ── what's next and the week ────────────────────────────────────────────────────────────────

def next_items(connection: Connection, project_id: str, onboarding: Mapping[str, Any],
               limit: int = 3) -> list[dict[str, Any]]:
    """WHAT'S NEXT: the top `limit` of the ranked backlog — open roadmap recommendations by
    score, then the onboarding agent's ranked candidates — each with its Accept (the roadmap's
    accept route, or THE start door with its body)."""
    from .titles import short_title

    # Accepted first (the Roadmap's Next lane, "Feature · accepted" with a Next tag), then the
    # open ones by score, each with its Accept.
    out: list[dict[str, Any]] = []
    for r in connection.execute(text(
            "SELECT r.work_item_id, r.title, r.why, r.kind FROM ambient_recommendations r "
            "LEFT JOIN work_items w ON w.id = r.work_item_id WHERE r.project_id = :p "
            "AND r.status = 'accepted' AND COALESCE(w.lifecycle, '') NOT IN "
            "('done', 'resolved', 'dismissed', 'in_progress', 'running') "
            "ORDER BY r.score DESC LIMIT :n"), {"p": project_id, "n": limit}).mappings():
        out.append({"id": r["work_item_id"], "label": short_title(r["title"], limit=5),
                    "detail": r["why"] or r["title"], "why": r["why"], "kind": r["kind"],
                    "source": "roadmap", "state": "next", "accepted": True,
                    "href": project_href(project_id, tab="roadmap",
                                         drawer=f"rec:{r['work_item_id']}"), "accept": None})
    recs = connection.execute(text(
        "SELECT work_item_id, title, why, kind, detector, facts->>'why_short' AS short, "
        "facts->>'external_url' AS url FROM ambient_recommendations "
        "WHERE project_id = :p AND status = 'open' ORDER BY score DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()

    out += [{"id": r["work_item_id"], "label": short_title(r["title"], limit=5),
            "detail": r["why"] or r["title"], "why": r["short"] or r["why"], "kind": r["kind"],
            "source": "onboarding" if r["detector"] == "onboarding" else "roadmap",
            "state": "proposed" if r["detector"] == "onboarding" else "next",
            "external_url": r["url"],
            "href": project_href(project_id, tab="roadmap", drawer=f"rec:{r['work_item_id']}"),
            "accept": {"href": f"/v1/ambient/roadmap/{r['work_item_id']}/accept", "method": "POST",
                       "body": {"as": "ticket", "project_id": project_id}}} for r in recs]
    out = out[:limit]
    # ONE SOURCE: onboarding's proposals ARE open recommendations (onboarding._propose_on_roadmap)
    # and came in above. A candidate of an older run that never became one is accepted the same
    # way — onto Roadmap › Next — never started from here.
    have = {o_["id"] for o_ in out}
    for c in (onboarding.get("backlog") or []):
        if len(out) >= limit:
            break
        if c.get("rec_id") or c.get("state") in ("accepted", "rejected", "gone") or \
                c["id"] in have:
            continue
        source = c.get("source") if c.get("source") in ("roadmap", "ticket", "bug", "ci") \
            else "roadmap"
        out.append({"id": c["id"], "label": short_title(c["label"], c.get("text"), limit=5),
                    "detail": c.get("detail"), "why": c.get("why"), "kind": c.get("kind"),
                    "source": source, "state": "proposed", "external_url": c.get("external_url"),
                    "href": c.get("href") or project_href(project_id, tab="roadmap"),
                    "accept": {"href": f"/v1/projects/{project_id}/onboarding/backlog/accept",
                               "method": "POST", "body": {"ids": [c["id"]]}}})
    return out[:limit]


def week(connection: Connection, project_id: str, now: datetime, limit: int = 6
         ) -> list[dict[str, Any]]:
    """THIS WEEK as a timeline: at most `limit` points, oldest first, ONE per thing that
    happened. A pull request is one event (merged beats opened, at its first build: every retry
    and follow-up carries the same PR and once drew "PR #3 opened" four times); a deploy is one
    per environment and commit; labels are ≤ 3 words."""
    from .titles import shorten

    since = now - timedelta(days=7)
    out: list[dict[str, Any]] = []
    seen_deploys: set[tuple[str, str]] = set()
    for r in connection.execute(text(
            "SELECT s.id, s.finished_at, s.commit_sha, t.environment FROM stack_deployments s "
            "JOIN deployment_targets t ON t.id = s.target_id WHERE s.project_id = :p "
            "AND s.status = 'healthy' AND s.finished_at >= :s ORDER BY s.finished_at DESC LIMIT 12"),
            {"p": project_id, "s": since}).mappings():
        key = (str(r["environment"] or ""), str(r["commit_sha"] or ""))
        if key in seen_deploys:
            continue
        seen_deploys.add(key)
        from .health import env_label

        out.append({"at": iso(r["finished_at"]), "kind": "deploy",
                    "label": f"{env_label(r['environment'])} deploy",
                    "href": href(project_id, area="infra", view="deploys", deploy=f"stack:{r['id']}")})
    prs: dict[int, dict[str, Any]] = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (n) n, id, url, opened FROM (SELECT COALESCE(pull_request_number, "
            "CAST(substring(pull_request_url from '/pull/([0-9]+)') AS INT)) AS n, id, "
            "pull_request_url AS url, created_at AS opened FROM code_task_runs WHERE project_id = :p "
            "AND COALESCE(pull_request_url, '') <> '') x WHERE n IS NOT NULL "
            "ORDER BY n, opened"), {"p": project_id}).mappings():
        if r["opened"] is not None and r["opened"] >= since:
            prs[int(r["n"])] = {"at": iso(r["opened"]), "kind": "pr",
                                "label": f"PR #{r['n']} opened", "external_url": r["url"],
                                "href": href(project_id, area="code_tasks", view="apps",
                                             build=r["id"])}
    for r in connection.execute(text(
            "SELECT id, approved_at, recommendation->>'pull_request_url' AS url FROM decisions "
            "WHERE project_id = :p AND status IN ('approved', 'executed') AND approved_at >= :s "
            "AND recommendation->>'action' = 'review_pull_request' ORDER BY approved_at"),
            {"p": project_id, "s": since}).mappings():
        n = pr_number(r["url"])
        if n is None:
            continue
        prs[n] = {"at": iso(r["approved_at"]), "kind": "merge", "label": f"PR #{n} merged",
                  "external_url": r["url"],
                  "href": href(project_id, area="decisions", decision=r["id"])}
    out.extend(prs.values())
    for r in connection.execute(text(
            "SELECT id, title, first_seen FROM incidents WHERE project_id = :p AND first_seen >= :s "
            "ORDER BY first_seen DESC LIMIT 4"), {"p": project_id, "s": since}).mappings():
        out.append({"at": iso(r["first_seen"]), "kind": "incident",
                    "label": shorten(r["title"], 3) or "Incident",
                    "href": project_href(project_id, tab="health")})
    for r in connection.execute(text(
            "SELECT id, COALESCE(title, directive) AS title, updated_at FROM meeting_runs "
            "WHERE project_id = :p AND status = 'completed' AND updated_at >= :s "
            "ORDER BY updated_at DESC LIMIT 4"), {"p": project_id, "s": since}).mappings():
        # A panel's plan reads "Plan agreed" on the dot (≤ 4 words, never a cut sentence);
        # its short title is the tooltip.
        out.append({"at": iso(r["updated_at"]), "kind": "idea", "label": "Plan agreed",
                    "detail": shorten(r["title"], 6),
                    "href": href(project_id, area="meetings", meeting=r["id"])})
    # Identical words on the same day are one point.
    seen: set[tuple[str, str]] = set()
    unique = []
    for e in sorted(out, key=lambda e: e["at"] or "", reverse=True):
        key = (e["label"], (e["at"] or "")[:10])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return sorted(unique[:limit], key=lambda e: e["at"] or "")


# ── the overview ────────────────────────────────────────────────────────────────────────────

def _facts(connection: Connection, project_id: str, items: list[dict[str, Any]],
           envs: list[dict[str, Any]], onboarding: Mapping[str, Any]) -> dict[str, Any]:
    healthy_non_prod = any(str(e["key"]).lower() not in PRODUCTION and e.get("deployed_at")
                           for e in envs)
    healthy_prod = any(str(e["key"]).lower() in PRODUCTION and e.get("deployed_at") for e in envs)
    has_recs = bool(connection.execute(text(
        "SELECT 1 FROM ambient_recommendations WHERE project_id = :p LIMIT 1"),
        {"p": project_id}).first())
    # The Work tab's Onboarding row is setup, not work: the journey's onboarding stage speaks for it.
    work = [w for w in items if w["source"] != "onboarding"]
    got = reached(onboarded=onboarding.get("state") in ("ready", "needs_you") or bool(work),
                  backlog=bool(onboarding.get("backlog")) or has_recs or bool(work),
                  worked=any(w["source"] != "roadmap" for w in work),
                  staging=healthy_non_prod, production=healthy_prod,
                  onboarding_running=onboarding.get("state") == "running")
    active = any(w["state"] in ("running", "needs_you") for w in work)
    return {"got": got, "active": active}


#: The Overview's cards, each of which loads or fails on its own (04 "Couldn't load · Retry").
CARDS = ("journey", "tile_work", "tile_running", "tile_staging", "tile_cost", "needs_you",
         "recent_work", "week", "captain", "next", "try_it")


def view(connection: Connection, project_id: str, *, now: Optional[datetime] = None
         ) -> dict[str, Any]:
    """The Overview's one read — but EACH CARD LOADS OR FAILS ON ITS OWN (screen 04 states): a
    store that fails takes only the cards it feeds, which come back named in `errors`
    ({card: words}) with their empty default; every other card still answers."""
    import logging

    from .deployments import live_url

    log = logging.getLogger(__name__)
    now = now or datetime.now(timezone.utc)
    project = require_project(connection, project_id)
    errors: dict[str, str] = {}

    def part(cards: tuple[str, ...], fn: Any, default: Any) -> Any:
        try:
            with connection.begin_nested():
                return fn()
        except Exception as exc:  # noqa: BLE001 — one card's store never blanks the page
            log.exception("overview: %s failed", "/".join(cards))
            for card in cards:
                errors.setdefault(card, f"Couldn't load ({type(exc).__name__})")
            return default

    roster = part(("captain",), lambda: agents(connection, project_id), {})
    onboarding = part(("journey",), lambda: onboarding_service.view(connection, project_id),
                      {"state": "not_started", "stages": [], "backlog": []})
    items = part(("tile_work", "tile_running", "recent_work", "journey"),
                 lambda: work_service.everything(connection, project_id, now=now,
                                                 onboarding=onboarding), [])
    envs = part(("tile_staging", "journey"),
                lambda: health_environments(connection, project_id, now=now), [])
    facts = part(("journey",), lambda: _facts(connection, project_id, items, envs, onboarding),
                 {"got": {}, "active": False})
    stage = stage_of(facts["got"], active=facts["active"])
    waiting = part(("needs_you",), lambda: needs_you(connection, project_id, items, onboarding),
                   [])
    the_repos = part(("journey",), lambda: repos(connection, project_id), [])
    url = live_url(envs) if envs else None
    logins = part(("try_it",), lambda: demo.listing(connection, project_id),
                  {"environments": [], "source": "none"})
    try_env = next((e for e in logins["environments"] if str(e["key"]).lower() in
                    ("staging", "stage")), None) or next(iter(logins["environments"]), None)
    subs, details, prod_waits = part(
        ("journey",), lambda: journey_subs(connection, project, items, envs, onboarding, waiting),
        ({}, {}, False))
    # Production waits on a person exactly when Needs you holds "Pick a prod host" (one rule,
    # needs.prod_waits), so the journey node and the card never disagree.
    prod_waits = any(n.get("id") == "prod:host" for n in waiting)
    cost = part(("tile_cost",), lambda: _cost_week(connection, project_id, now), None)
    the_tiles = tiles(project_id, items, envs, cost)   # cost None → the tile's "Couldn't load"
    return {
        "project": {"id": project["id"], "name": project["name"], "initials":
                    initials(project["name"]), "description": project["description"],
                    "stage": stage, "repos": the_repos, "live_url": url,
                    "health": rollup(envs), "status_label": STATUS_LABELS[stage],
                    "archived": project["archived_at"] is not None},
        "journey": journey(project_id, facts["got"], stage,
                           needs_you=len(waiting), subs=subs, details=details,
                           prod_waits=prod_waits,
                           active=facts["active"] or bool(facts["got"].get("_onboarding_running"))),
        "needs_you": waiting,
        # The Roadmap tab's count: what is proposed or accepted and not done yet.
        "roadmap_count": part(("next",), lambda: int(connection.execute(text(
            "SELECT count(*) FROM ambient_recommendations WHERE project_id = :p "
            "AND status IN ('open', 'accepted')"), {"p": project_id}).scalar() or 0), None),
        "tiles": the_tiles,
        "captain": part(("captain",), lambda: captain_service.view(
            connection, project_id, roster=roster, with_last_said=False), None),
        # Recent WORK (screen 04): what moved — running, waiting on you, done — before roadmap
        # items that are only planned; at most five, newest first within that order.
        "recent_work": ([w for w in items if w["state"] != "planned"]
                        + [w for w in items if w["state"] == "planned"])[:5],
        "next": part(("next",), lambda: next_items(connection, project_id, onboarding), []),
        "try_it": {"url": url, "env": try_env["key"] if try_env else None,
                   "demo_logins": try_env["logins"] if try_env else [],
                   "users_from": logins["source"]},
        "onboarding": {"state": onboarding["state"], "stages": onboarding["stages"],
                       "href": onboarding.get("href") or project_href(project_id,
                                                                      tab="onboarding")},
        "week": part(("week",), lambda: week(connection, project_id, now), []),
        # Cards that could not load this time, in words: each shows "Couldn't load · Retry".
        "errors": errors,
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
    # One presentation everywhere (cost_words): on a subscription the card says so, with the
    # list price, never a bare $0.00.
    return {"cost_week": cost["usd"], "series": cost["series"],
            "on_subscription": cost.get("on_subscription", False),
            "list_week": cost.get("list_usd"), "list_series": cost.get("list_series") or []}


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
        # The same count as the project's Needs-you bar and the bell: decisions waiting on a
        # person plus what only a person can set up (needs.setup_cards).
        from .needs import setup_cards

        try:
            with connection.begin_nested():
                setup_n = len(setup_cards(connection, pid))
        except Exception:  # noqa: BLE001 — a card never fails over its setup line
            setup_n = 0
        decided = count_waiting_on_operator(connection, pid, now=now)
        waiting = decided + setup_n
        live = int(run.get("live") or 0) + int(build.get("live") or 0)
        worked = int(run.get("total") or 0) + int(build.get("total") or 0) > 0
        non_prod = any(t["deployed"] and str(t["environment"]).lower() not in PRODUCTION
                       for t in tgs)
        prod = any(t["deployed"] and str(t["environment"]).lower() in PRODUCTION for t in tgs)
        got = reached(onboarded=onb.get("state") in ("ready", "needs_you") or worked,
                      backlog=bool(onb.get("backlog")) or pid in recs or worked, worked=worked,
                      staging=non_prod, production=prod,
                      onboarding_running=onb.get("state") == "running")
        # WORKING ONLY WHILE WORK MOVES: a running build or run, or work waiting on a person's
        # decision. A setup card ("Pick a prod host") is Needs you, not work in progress.
        moving = bool(live or decided) and worked
        stage = stage_of(got, active=moving)
        health_words = ["healthy" if t["ok"] else "down" for t in tgs
                        if t["deployed"] and t["ok"] is not None]
        health = ("down" if "down" in health_words and "healthy" not in health_words else
                  "degraded" if "down" in health_words else
                  "healthy" if health_words else "unknown")
        ordered = sorted(tgs, key=lambda t: (str(t["environment"]).lower() not in ("staging",
                                                                                  "stage"),
                                             str(t["environment"]).lower() in PRODUCTION))
        url = web_url(next((t["open_url"] or t["public_url"] or ((t["domains"] or [None])[0]
                                                                 if isinstance(t["domains"], list)
                                                                 else None)
                            for t in ordered if t["deployed"]), None))
        roster = roles.get(pid, {})
        key = captains.get(pid) if captains.get(pid) in roster else captain_service.default_role(roster)
        f = face(roster, key) if key else None
        updated = max([v for v in (run.get("updated"), build.get("updated"), p["created_at"])
                       if v is not None])
        # "Onboarding · Helm is reading it" only while an onboarding job is MOVING; a project
        # that never ran one (or whose run ended) is "Not onboarded", with Start onboarding.
        onboarding_active = onb.get("state") == "running"
        status = STATUS_LABELS[stage]
        if stage == "working" and not moving:
            status = "Idle"
        if stage == "onboarding" and not onboarding_active:
            status = "Not onboarded" if onb.get("state") in (None, "not_started", "failed") \
                else "Ready for work"
        out.append({"id": pid, "name": p["name"], "initials": initials(p["name"]),
                    "repo": primary.get(pid), "stage": stage,
                    "status_label": status, "onboarding_active": onboarding_active,
                    "start_onboarding": (None if onboarding_active else
                                         {"href": f"/v1/projects/{pid}/onboarding/start",
                                          "method": "POST"}),
                    "journey": [{"key": j["key"], "label": j["label"], "state": j["state"]}
                                for j in journey(pid, got, stage, needs_you=waiting,
                                                 active=moving or onboarding_active)],
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
