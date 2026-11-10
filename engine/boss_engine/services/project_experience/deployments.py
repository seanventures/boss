"""Deployments tab (contract §5): each environment of the project with its URL, health, last
deploy, demo logins and release policy, and whether production is set up.

Reads deployment_targets, deployment_health.health_view (the probe samples), stack_deployments
(the newest deploy, who and which commit), deployment_hosts (where it runs) and the demo-login
listing (names only). Production is never automatic: a target in production is `person`.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import demo_logins as demo
from .common import href, iso, project_href
from .health import env_label, health_word

PRODUCTION = ("production", "prod")


def _last_deploys(connection: Connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not target_ids:
        return {}
    # WHAT IS LIVE, AND SINCE WHEN. The newest LIVE deploy by the time it went live — not the
    # newest row: a re-observation or a backfill writes a row for a commit already live, and its
    # write time said "Last deploy PR #3 · 1m ago" beside History's real "Fri 10:55". A target
    # with nothing live yet shows its newest attempt.
    rows = connection.execute(text(
        "SELECT DISTINCT ON (target_id) id, target_id, commit_sha, ref, status, created_by, "
        "created_at, finished_at, open_url, workflow_run_id FROM stack_deployments "
        "WHERE target_id = ANY(:ids) "
        "ORDER BY target_id, (status = 'healthy') DESC, COALESCE(finished_at, created_at) DESC"),
        {"ids": target_ids}).mappings().all()
    out = {str(r["target_id"]): dict(r) for r in rows}
    live = [(t, str(d["commit_sha"] or "")) for t, d in out.items()
            if d["status"] == "healthy" and d["commit_sha"]]
    for target, sha in live:
        first = connection.execute(text(
            "SELECT id, created_by, COALESCE(finished_at, created_at) AS at FROM stack_deployments "
            "WHERE target_id = :t AND commit_sha = :c AND status = 'healthy' "
            "ORDER BY COALESCE(finished_at, created_at) LIMIT 1"),
            {"t": target, "c": sha}).mappings().first()
        if first:
            out[target]["live_at"] = first["at"]
            out[target]["first_id"] = first["id"]
            out[target]["first_by"] = first["created_by"]
    return out


def hourly(connection: Connection, target_ids: list[str], now: Any = None
           ) -> dict[str, list[dict[str, Any]]]:
    """THE 24 H STRIP (08-08): per target, 24 hourly bars, oldest first — each hour's probes,
    the share that answered, and its state: `healthy`, `degraded` (some failed), `down` (most
    failed) or `none` (no probe that hour, never a false healthy)."""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(hours=24)
    out: dict[str, list[dict[str, Any]]] = {}
    for t in target_ids:
        out[t] = [{"at": iso(start + timedelta(hours=i)), "n": 0, "ok": 0, "state": "none"}
                  for i in range(24)]
    if not target_ids:
        return out
    for r in connection.execute(text(
            "SELECT deployment_target_id AS t, floor(extract(epoch FROM (checked_at - :s)) / 3600) "
            "AS h, count(*) AS n, sum(CASE WHEN ok THEN 1 ELSE 0 END) AS okn "
            "FROM deployment_health_samples WHERE deployment_target_id = ANY(:ids) "
            "AND checked_at >= :s AND checked_at < :e GROUP BY 1, 2"),
            {"ids": target_ids, "s": start, "e": end}).mappings():
        i = int(r["h"])
        if 0 <= i < 24:
            n, ok = int(r["n"]), int(r["okn"] or 0)
            share = ok / n if n else 0
            out[str(r["t"])][i].update(n=n, ok=ok, state=(
                "healthy" if share >= 0.99 else "down" if share < 0.5 else "degraded"))
    return out


#: A deploy's state in words (the raw status stays in `status`): Infra's own reading
#: (infra_view.deploy_status), so the two screens never disagree.
def _state_words(status: Any, decision_status: Any) -> tuple[str, str]:
    from ..infra_view import deploy_status

    try:
        got = deploy_status(status, decision_status=decision_status)
        return str(got.get("word") or status), str(got.get("tone") or "grey")
    except Exception:  # noqa: BLE001 — words are a nicety; the raw state is still there
        return str(status or "").replace("_", " ").capitalize(), "grey"


def _pr_by_commit(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """commit → {number, title}: the pull request whose recorded merge it is, and its short
    title (titles.short_title of the build's title and brief)."""
    from ..repo_links import change_title, pr_link
    from .common import pr_number
    from .titles import short_title

    out: dict[str, dict[str, Any]] = {}
    titled: dict[str, Optional[str]] = {}
    for r in connection.execute(text(
            "SELECT finalize_ctx->'merge'->>'sha' AS sha, pull_request_number AS n, "
            "pull_request_url AS u, title, spec->>'instructions' AS brief FROM code_task_runs "
            "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
            "ORDER BY created_at"), {"p": project_id}).mappings():
        n = r["n"] or pr_number(r["u"])
        if n and str(r["sha"]) not in out:
            # A local change set's URL (http://boss-local/…) opens BOSS's change-set view.
            # The CHANGE SET's own title, never the follow-up build's that recorded the merge.
            url = str(r["u"] or "")
            if url and url not in titled:
                titled[url] = change_title(connection, url)
            own = titled.get(url)
            out[str(r["sha"])] = {"number": int(n), "url": pr_link(r["u"]),
                                  "title": short_title(own) if own
                                  else short_title(r["title"], r["brief"])}
    return out


def _duration(start: Any, end: Any) -> Optional[str]:
    if start is None or end is None:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _history(connection: Connection, project_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent deploys in words (screen 08): "Staging · PR #2 UI refresh", "Wed 16:40 · 3m 10s ·
    smoke ✓", Live / Needs you / Failed, WHO by name (people_names), never a principal id or a
    raw status. A request that never ran because a later deploy overtook it (superseded) is
    not history."""
    from ..people_names import who

    rows = connection.execute(text(
        "SELECT s.id, s.target_id, s.commit_sha, s.status, s.created_by, s.created_at, "
        "s.apply_started_at, s.finished_at, s.failure_reason, s.rollback_of, "
        "t.environment, t.app_name, d.status AS decision_status, d.approved_by "
        "FROM stack_deployments s JOIN deployment_targets t ON t.id = s.target_id "
        "LEFT JOIN decisions d ON d.id = s.decision_id "
        "WHERE s.project_id = :p ORDER BY s.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit * 3}).mappings().all()
    prs = _pr_by_commit(connection, project_id)
    out = []
    newest: set[str] = set()
    live_after: dict[str, Any] = {}
    firsts = {str(t): at for t, at in connection.execute(text(
        "SELECT target_id, min(created_at) FROM stack_deployments WHERE project_id = :p "
        "GROUP BY target_id"), {"p": project_id}).all()}
    for r in rows:
        if r["status"] == "healthy":
            t = str(r["target_id"])
            live_after[t] = max(live_after.get(t) or r["created_at"], r["created_at"])
    for r in rows:
        word, tone = _state_words(r["status"], r["decision_status"])
        if word == "Superseded":
            continue
        if r["status"] == "healthy":
            if r["target_id"] in newest:
                word = "Done"
            newest.add(r["target_id"])
        pr = prs.get(str(r["commit_sha"] or ""))
        first_ever = r["created_at"] == firsts.get(str(r["target_id"]))
        what = (f"PR #{pr['number']} {pr['title']}".strip() if pr else
                "rollback" if r["rollback_of"] else "first deploy" if first_ever
                else (r["commit_sha"] or "")[:7])
        actor = r["approved_by"] or r["created_by"]
        state = ("done" if r["status"] == "healthy" else "failed" if r["status"] == "failed"
                 else "waiting" if r["status"] == "awaiting_approval" else
                 "skipped" if r["status"] == "rejected" else "now")
        recovered = (r["status"] == "failed" and live_after.get(str(r["target_id"])) is not None
                     and live_after[str(r["target_id"])] > r["created_at"])
        if recovered:
            state = "recovered"
        took = _duration(r["apply_started_at"], r["finished_at"])
        smoke = ("smoke ✓" if r["status"] == "healthy" else
                 "smoke ✗" if r["status"] == "failed" and r["apply_started_at"] else None)
        reason = None if r["status"] in ("healthy", "awaiting_approval") else \
            (" ".join(str(r["failure_reason"] or "").split())[:110].rstrip(".") or None)
        out.append({"id": r["id"], "label": f"{env_label(r['environment'])} · {what}".strip(" ·"),
                    "env": r["environment"], "app": r["app_name"], "status": r["status"],
                    "state": state, "state_word": word, "tone": tone,
                    "pr": pr["number"] if pr else None, "title": pr["title"] if pr else None,
                    "commit": (r["commit_sha"] or "")[:7] or None,
                    "duration": None if recovered else took, "smoke": None if recovered else smoke,
                    "reason": reason, "recovered": recovered,
                    "by": who(actor, connection) if actor else None,
                    "by_agent": str(actor or "") in ("boss", "auto_approval") or
                    str(actor or "").startswith(("agt_", "policy:")),
                    "at": iso(r["finished_at"] or r["created_at"]),
                    "href": href(project_id, area="infra", view="deploys", deploy=f"stack:{r['id']}")})
        if len(out) >= limit:
            break
    # A DEPLOY THAT FAILED BEFORE IT HAD A RECORD (deploy_truth.step_failures) is history too:
    # History said "No deploys yet" beside a dead-lettered deploy step.
    from ..deploy_truth import step_failures

    failed = step_failures(connection, project_id=project_id, limit=limit)
    if failed:
        envs = {str(r["id"]): r for r in connection.execute(text(
            "SELECT id, environment, app_name FROM deployment_targets WHERE id = ANY(:i)"),
            {"i": sorted({str(f["target_id"]) for f in failed if f["target_id"]})}).mappings()}
        for f in failed:
            t = envs.get(str(f["target_id"] or "")) or {}
            commit = str(f["commit"] or "")[:7] or None
            pr = prs.get(str(f["commit"] or ""))
            what = (f"PR #{pr['number']} {pr['title']}".strip() if pr else commit or "deploy")
            label = f"{env_label(t['environment'])} · {what}" if t else what
            out.append({"id": f"step:{f['id']}", "label": label.strip(" ·"),
                        "env": t.get("environment"), "app": t.get("app_name"),
                        "status": "failed", "state": "failed", "state_word": "Failed",
                        "tone": "red", "pr": pr["number"] if pr else None,
                        "title": pr["title"] if pr else None, "commit": commit,
                        "duration": None, "smoke": None,
                        "reason": f["reason"][:110].rstrip(".") or None, "recovered": False,
                        "by": None, "by_agent": True, "at": iso(f["created_at"]),
                        "workflow_run_id": f["workflow_run_id"], "step_id": f["step_id"],
                        "href": href(project_id, area="delivery_workflows",
                                     wfr=f["workflow_run_id"])})
        out.sort(key=lambda h: str(h.get("at") or ""), reverse=True)
        out = out[:limit]
    return out


def host_label(name: Any, address: Any, environment: Any = None) -> str:
    """A server's NAME for a title: its own name, unless that is only its address (an IP or a
    bare host), then "<Env> server"; the address stays a mono sub-line beside it."""
    import re as _re

    n = str(name or "").strip()
    a = str(address or "").strip()
    if n and n != a and not _re.match(r"^\d{1,3}(\.\d{1,3}){3}$", n):
        return n
    return f"{env_label(environment)} server" if environment else "Server"


def _who_name(connection: Connection, actor: Any) -> Optional[str]:
    from ..people_names import who

    return who(actor, connection) if actor else None


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    from ..deploy_truth import project_targets, truth
    from ..deployment_health import health_view

    # ONE SOURCE for the project's targets (deploy_truth.project_targets): the same rows the
    # header, Health and the agents read, a superseded staging slot left out.
    targets = project_targets(connection, project_id)
    hosts_by_id = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT id, name, address, status FROM deployment_hosts WHERE id = ANY(:h)"),
        {"h": sorted({str(t["host_id"]) for t in targets if t.get("host_id")})}).mappings()}
    for t in targets:
        h = hosts_by_id.get(str(t.get("host_id") or "")) or {}
        t.update(host_name=h.get("name"), host_address=h.get("address"),
                 host_status=h.get("status"))
    targets.sort(key=lambda t: (str(t["environment"] or "").lower() in PRODUCTION,
                                str(t["environment"] or ""), str(t["app_name"] or "")))
    told = truth(connection, targets)
    health = {t["target_id"]: t for t in health_view(connection, project_id).get("targets") or []}
    # ONE SOURCE for health, uptime and p95: the Health tab's environments (health.state_of,
    # obs_view.probe_window over 24 h), so the header, Overview, Health, Deployments and
    # Observability show the same word and the same number.
    from .health import environments as health_environments

    same = {e["target_id"]: e for e in health_environments(connection, project_id)}
    last = _last_deploys(connection, [t["id"] for t in targets])
    hours = hourly(connection, [t["id"] for t in targets])
    prs = _pr_by_commit(connection, project_id)
    host_ids = sorted({str(t["host_id"]) for t in targets if t["host_id"]})
    shared = {str(r["host_id"]): int(r["n"]) for r in connection.execute(text(
        "SELECT host_id, count(DISTINCT project_id) AS n FROM deployment_targets "
        "WHERE host_id = ANY(:h) AND status = 'active' GROUP BY host_id"),
        {"h": host_ids}).mappings()} if host_ids else {}
    logins = demo.listing(connection, project_id)
    by_env = {e["key"]: e["logins"] for e in logins["environments"]}
    apps = {t["app_name"] for t in targets}
    envs = []
    for t in targets:
        h = health.get(t["id"], {})
        one = same.get(t["id"], {})
        d = last.get(t["id"])
        said = told[str(t["id"])]
        # LIVE ONLY AFTER A DEPLOY SUCCEEDED (BOSSAPP-930): before that the address is words
        # ("Setting up · will answer at …"), never a link, and never the header's live chip.
        url = said["live_url"]
        label = env_label(t["environment"])
        if len(apps) > 1:
            label = f"{t['app_name']} {label}"
        is_prod = str(t["environment"] or "").lower() in PRODUCTION
        envs.append({
            "key": t["environment"], "label": label, "app": t["app_name"], "target_id": t["id"],
            "url": url, "address": said["address"],
            # THE DEPLOY'S TRUTH (deploy_truth.truth): state + words, the failed reason, and
            # the running deploy's seven phases for the live card.
            "deploy": {k: said[k] for k in ("state", "word", "live", "reason", "attempt",
                                            "progress")},
            "health": one.get("health") or health_word(h.get("status")),
            "health_reason": one.get("health_reason"), "status": h.get("status"),
            "uptime_pct": one.get("uptime_pct") if one else h.get("uptime_24h"),
            "p95_ms": one.get("p95_ms") if one else None,
            "p95_source": one.get("p95_source", "probe"), "window": one.get("window", "24 h"),
            "error_rate": one.get("error_rate"),
            # The 24 h strip: each point's share of good answers (1 = all good).
            "series": [round(max(0.0, 1 - float(v) / 100.0), 3)
                       for v in ((one.get("series") or {}).get("errors") or [])],
            # 24 hourly bars from the probes (oldest first): healthy · degraded · down · none.
            "hours": hours.get(t["id"]) or [],
            "host": t["host_address"] or t["host_name"], "host_status": t["host_status"],
            "host_name": host_label(t["host_name"], t["host_address"], t["environment"]),
            "host_projects": shared.get(str(t["host_id"]), 1) if t["host_id"] else None,
            # The server, in Infra (shared across projects): its drawer, which links back here.
            "host_id": t["host_id"],
            "host_href": href(project_id, area="infra", host=t["host_id"]) if t["host_id"] else None,
            "last_deploy": ({"commit": (d["commit_sha"] or "")[:7] or None, "status": d["status"],
                             "pr": (prs.get(str(d["commit_sha"] or "")) or {}).get("number"),
                             "pr_url": (prs.get(str(d["commit_sha"] or "")) or {}).get("url"),
                             # When it went live (the first live row of this commit).
                             "at": iso(d.get("live_at") or d["finished_at"] or d["created_at"]),
                             "by": _who_name(connection, d.get("first_by") or d["created_by"]),
                             "href": href(project_id, area="infra", view="deploys",
                                          deploy=f"stack:{d.get('first_id') or d['id']}")}
                            if d else None),
            "demo_logins": by_env.get(t["environment"]) or [],
            "policy": "person" if is_prod or not t["auto_deploy"] else "auto",
            "paused": bool(t["paused"]),
            "href": project_href(project_id, tab="deployments", drawer=f"env:{t['id']}"),
            "manage_href": href(project_id, area="infra", target=t["id"]),
        })
    has_prod = any(str(e["key"] or "").lower() in PRODUCTION for e in envs)
    if has_prod:
        production: dict[str, Any] = {"state": "ready", "action": None}
    else:
        from ..deployment_fleet import usable_hosts_sql

        hosts = int(connection.execute(text(
            f"SELECT count(*) FROM deployment_hosts WHERE status = 'verified' AND "
            f"{usable_hosts_sql()}"), {"p": project_id}).scalar() or 0)
        # Infra opens its own + Add sheet from `add=` (host, or an app target; with no machine
        # yet it asks for the host first).
        production = ({"state": "needs_host",
                       "action": {"label": "Add a server", "href": href(project_id, area="infra",
                                                                         add="server")}}
                      if not hosts else
                      {"state": "none",
                       "action": {"label": "Set up production",
                                  "href": href(project_id, area="infra", add="target",
                                               env="production")}})
    preview = next((e for e in logins["environments"] if e["key"] == "preview"), None)
    # WHERE A READY SERVER SHOWS UP (Add a server never moves an app): every verified server this
    # project may use, with its role. The app picks one here; production stays a person's choice.
    from ..add_server import servers_for_project

    has_staging = any(str(e["key"] or "").lower() not in PRODUCTION for e in envs)
    servers = []
    for sv in servers_for_project(connection, project_id):
        if sv["role"] == "prod":
            act = {"label": "Use for production", "href": href(project_id, area="infra", add="target",
                                                               host=sv["host_id"], env="production"),
                   "do": None}
        elif not has_staging:
            act = {"label": "Use for staging", "do": "staging_target", "host_id": sv["host_id"],
                   "post": f"/v1/projects/{project_id}/onboarding/deploy-target",
                   "href": href(project_id, area="infra", add="target", host=sv["host_id"], env="staging")}
        else:
            act = {"label": "Add an environment", "href": href(project_id, area="infra", add="target",
                                                              host=sv["host_id"]), "do": None}
        servers.append({**sv, "state": "ready", "href": href(project_id, area="infra", host=sv["host_id"]),
                        "action": act})
    out = {"environments": envs, "production": production, "ship_path": ship_path(envs),
           "servers": servers, "add_server_href": href(project_id, area="infra", add="server"),
           "preview": ({"demo_logins": preview["logins"]} if preview else None),
           "demo_logins": logins, "history": _history(connection, project_id)}
    failed_first = next((h for h in out["history"] if h["state"] == "failed"
                         and str(h["id"]).startswith("step:")), None)
    if not envs and failed_first:
        # The first deploy failed before it had a target or a record: say so, never "Not
        # deployed yet", with the run whose step a Retry puts back (deploy_truth.step_failures).
        out["empty"] = {"label": "Last deploy failed", "detail": failed_first["reason"] or "",
                        "href": failed_first["href"], "failed": True,
                        "workflow_run_id": failed_first["workflow_run_id"],
                        "step_id": failed_first["step_id"]}
    elif not envs:
        out["empty"] = {"label": "Not deployed yet",
                        "detail": "The first merged work deploys to staging by the project's "
                                  "policy; production always waits for a person.",
                        "href": href(project_id, area="infra", add="target")}
    return out


def _went_live(env: Optional[dict[str, Any]]) -> bool:
    """A deploy to this environment SUCCEEDED (deploy_truth) — a failed or waiting attempt is a
    last deploy, never a shipped one."""
    if not env:
        return False
    told = env.get("deploy")
    return bool(told.get("live")) if isinstance(told, dict) else bool(env.get("last_deploy"))


def ship_path(envs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """How code ships, as a strip (screen 8): merge → build → the first non-production
    environment (auto or a person, from its target's policy) → its health check → production,
    always a person's approval. States come from what the environments show today."""
    pre = next((e for e in envs if str(e["key"] or "").lower() not in PRODUCTION), None)
    prod = next((e for e in envs if str(e["key"] or "").lower() in PRODUCTION), None)
    shipped = _went_live(pre)
    nodes = [{"label": "PR merged", "state": "done" if shipped else "todo"},
             {"label": "Image built", "state": "done" if shipped else "todo"}]
    if pre is None:
        nodes.append({"label": "Staging · not set", "state": "todo"})
        nodes.append({"label": "Smoke test", "state": "todo"})
    else:
        who = "auto" if pre["policy"] == "auto" else "you approve"
        nodes.append({"label": f"{env_label(pre['key'])} · {who}",
                      "state": "done" if shipped else ("waiting" if who != "auto" else "todo")})
        nodes.append({"label": "Smoke test", "state": {"healthy": "done", "down": "failed"}.get(
            pre["health"], "todo") if shipped else "todo"})
    nodes.append({"label": "Prod · you approve",
                  "state": "done" if _went_live(prod) else "waiting"})
    return nodes


def live_url(envs: list[dict[str, Any]]) -> Optional[str]:
    """The URL the header chip opens: staging first, then any non-production, then prod."""
    ordered = sorted(envs, key=lambda e: (str(e["key"]).lower() not in ("staging", "stage"),
                                          str(e["key"]).lower() in PRODUCTION))
    return next((e["url"] for e in ordered if e.get("url")), None)


__all__ = ["PRODUCTION", "host_label", "live_url", "ship_path", "view"]
