"""Health tab (contract §12): one project's environments, alerts, incidents and monitoring.

Built only from what BOSS already records:
  environments   deployment_health.health_view (the probe samples: status, uptime, p95, series)
                 + app_metrics_scrapes (the app's own requests/errors/p95, when it exposes them)
  alerts         app_alerts (the alert engine's rows for this project)
  incidents      the incident squad's work items (ambient board, task classes incident_*), each
                 linked to its Agents-at-work graph; open incidents the squad never picked up too
  pack           app_alert_rules (Beacon's monitoring pack lives in boss.deploy.yaml: source yaml)
  links          observability_stack.endpoint_state (a Grafana link only when it answered)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import web_url, as_json, href, iso, project_href, words

#: The availability objective an environment is held to when nobody set one.
DEFAULT_SLO = 99.5
#: Points in each sparkline.
POINTS = 30

_HEALTH = {"up": "healthy", "degraded": "degraded", "down": "down"}
_SEVERITY = {"sev1": "page", "sev2": "page", "sev3": "ticket", "sev4": "info"}
_ENV_LABELS = {"preview": "Preview", "staging": "Staging", "stage": "Staging",
               "production": "Production", "prod": "Production", "dev": "Dev",
               "development": "Dev", "test": "Test", "qa": "QA"}


def env_label(environment: Any) -> str:
    key = str(environment or "").strip().lower()
    return _ENV_LABELS.get(key) or (key[:1].upper() + key[1:] if key else "Environment")


def health_word(status: Any) -> str:
    """deployment_health's verdict → the shared `health` enum."""
    return _HEALTH.get(str(status or ""), "unknown")


def burn(uptime: Optional[float], target: float = DEFAULT_SLO) -> str:
    """ok | warning | breach: the objective met, within half a point of it, or missed."""
    if uptime is None:
        return "ok"
    if uptime >= target:
        return "ok"
    if uptime >= target - 0.5:
        return "warning"
    return "breach"


def budget_used(uptime: Optional[float], target: float = DEFAULT_SLO) -> Optional[int]:
    """How much of the error budget the last 24 h used: downtime over the downtime the SLO
    allows (99.5 % allows 0.5 %), as a whole percent, capped at 100."""
    if uptime is None:
        return None
    allowed = 100.0 - target
    if allowed <= 0:
        return 100 if uptime < 100 else 0
    return int(round(min(100.0, max(0.0, (100.0 - float(uptime)) * 100.0 / allowed))))


def _metrics(connection: Connection, target_ids: list[str], since: datetime
             ) -> dict[str, dict[str, Any]]:
    """Per target: the app's own error rate (%) and p95 over 24 h, and the newest points."""
    if not target_ids:
        return {}
    rows = connection.execute(text(
        "SELECT deployment_target_id AS t, scraped_at, requests, errors, p95_ms FROM ("
        " SELECT deployment_target_id, scraped_at, requests, errors, p95_ms, row_number() OVER ("
        "  PARTITION BY deployment_target_id ORDER BY scraped_at DESC) AS n "
        " FROM app_metrics_scrapes WHERE deployment_target_id = ANY(:ids) AND ok "
        " AND scraped_at >= :since) s WHERE n <= 200 ORDER BY scraped_at"),
        {"ids": target_ids, "since": since}).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = out.setdefault(str(r["t"]), {"requests": 0.0, "errors": 0.0, "p95": [], "errors_s": []})
        req, err = float(r["requests"] or 0), float(r["errors"] or 0)
        m["requests"] += req
        m["errors"] += err
        if r["p95_ms"] is not None:
            m["p95"].append(float(r["p95_ms"]))
        m["errors_s"].append(round(err * 100.0 / req, 2) if req else 0.0)
    for m in out.values():
        m["error_rate"] = round(m["errors"] * 100.0 / m["requests"], 2) if m["requests"] else None
        m["p95_ms"] = round(sorted(m["p95"])[int(0.95 * (len(m["p95"]) - 1))]) if m["p95"] else None
        m["latency_s"] = [round(v) for v in m["p95"][-POINTS:]]
        m["errors_s"] = m["errors_s"][-POINTS:]
    return out


#: The window every uptime and p95 on the project pages is measured over (said beside them).
WINDOW_WORD = "24 h"


def state_of(probe: Any, uptime: Optional[float], firing: list[str],
             target: float = DEFAULT_SLO) -> tuple[str, Optional[str]]:
    """THE HEALTH OF ONE ENVIRONMENT, decided once (12-05): the probe's verdict, the SLO over
    the last 24 h and what is firing — never "Healthy" beside a breached SLO or a firing alert.
    → (healthy | degraded | down | unknown, the reason in words or None)."""
    verdict = health_word(probe)
    if verdict == "down":
        return "down", "Not answering its health check"
    if verdict == "unknown":
        return "unknown", None
    b = burn(uptime, target)
    if b == "breach":
        return "degraded", f"SLO breached: {uptime:g}% up, target {target:g}%"
    if firing:
        more = f" (+{len(firing) - 1} more)" if len(firing) > 1 else ""
        return "degraded", f"{firing[0]} firing{more}"
    if verdict == "degraded":
        return "degraded", "Slow or failed health checks"
    if b == "warning":
        return "degraded", f"SLO at risk: {uptime:g}% up, target {target:g}%"
    return "healthy", None


def firing_alerts(connection: Connection, target_ids: list[str]) -> dict[str, list[str]]:
    """Per target, the names of the app's alerts firing now (BOSS's own platform alerts are not
    the app's), in the same words as the Alerts card."""
    if not target_ids:
        return {}
    out: dict[str, list[str]] = {}
    for r in connection.execute(text(
            "SELECT COALESCE(a.subject_id, a.target_id) AS t, a.alertname, a.rule_kind, "
            "a.annotations FROM app_alerts a WHERE COALESCE(a.subject_id, a.target_id) = ANY(:ids) "
            "AND a.state = 'firing' AND COALESCE(a.labels->>'layer', '') <> 'boss' "
            "AND COALESCE(a.labels->>'boss_meta', '') <> 'true' "
            "ORDER BY COALESCE(a.fired_at, a.active_at) DESC"), {"ids": target_ids}).mappings():
        ann = as_json(r["annotations"], {}) or {}
        name = alert_name(ann.get("summary"), r["alertname"], r["rule_kind"])
        got = out.setdefault(str(r["t"]), [])
        if name not in got:
            got.append(name)
    return out


def target_states(connection: Connection, target_ids: list[str], *,
                  now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Batched (the Projects list): per target, THE health (state_of) from its newest probes, its
    24 h uptime (obs_view.probe_window, the one source) and what fires."""
    from ..deployment_health import verdict
    from ..obs_view import probe_window

    if not target_ids:
        return {}
    newest: dict[str, list[dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT t, ok, latency_ms FROM (SELECT deployment_target_id AS t, ok, latency_ms, "
            " checked_at, row_number() OVER (PARTITION BY deployment_target_id "
            " ORDER BY checked_at DESC) AS n FROM deployment_health_samples "
            " WHERE deployment_target_id = ANY(:ids) AND checked_at >= :since) s "
            "WHERE n <= 10 ORDER BY t, checked_at DESC"),
            {"ids": target_ids, "since": (now or datetime.now(timezone.utc)) - timedelta(days=7)}
    ).mappings():
        newest.setdefault(str(r["t"]), []).append(dict(r))
    window = probe_window(connection, target_ids, now=now)
    fires = firing_alerts(connection, target_ids)
    out = {}
    for tid in target_ids:
        up = window.get(tid, {}).get("uptime")
        pct = round(up * 100, 2) if up is not None else None
        state, why = state_of(verdict(newest.get(tid, [])), pct, fires.get(tid, []))
        out[tid] = {"health": state, "health_reason": why, "uptime_pct": pct,
                    "p95_ms": window.get(tid, {}).get("p95_ms")}
    return out


def environments(connection: Connection, project_id: str, *,
                 now: Optional[datetime] = None) -> list[dict[str, Any]]:
    from ..deployment_health import health_view
    from ..obs_view import probe_window

    now = now or datetime.now(timezone.utc)
    view = health_view(connection, project_id, now=now, points=POINTS)
    targets = view.get("targets") or []
    ids = [t["target_id"] for t in targets]
    metrics = _metrics(connection, ids, now - timedelta(hours=24))
    # ONE SOURCE for uptime and p95 (15-03 / PX-09): BOSS's probes over the last 24 h, the same
    # numbers Observability's app row prints; the window is said beside them.
    window = probe_window(connection, ids, now=now)
    fires = firing_alerts(connection, ids)
    hosts = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT t.id, t.host_id, h.address, h.name FROM deployment_targets t "
        "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.id = ANY(:ids)"),
        {"ids": ids}).mappings()} if targets else {}
    apps = {t["app_name"] for t in targets}
    out = []
    for t in targets:
        m = metrics.get(t["target_id"], {})
        w = window.get(t["target_id"], {})
        uptime = round(w["uptime"] * 100, 2) if w.get("uptime") is not None else None
        label = env_label(t["environment"])
        if len(apps) > 1:
            label = words(f"{t['app_name']} {label}")
        latency = [s["ms"] for s in t.get("series") or [] if s.get("ms") is not None][-POINTS:]
        errors = m.get("errors_s") or [0.0 if s.get("ok") else 100.0
                                       for s in t.get("series") or []][-POINTS:]
        state, why = state_of(t["status"], uptime, fires.get(t["target_id"], []))
        out.append({
            "key": str(t["environment"]), "label": label, "app": t["app_name"],
            "target_id": t["target_id"], "url": web_url(t.get("url")),
            "health": state, "health_reason": why, "status": t["status"],
            "uptime_pct": uptime, "error_rate": m.get("error_rate"),
            "p95_ms": w.get("p95_ms"),
            # Where the p95 comes from and over what, said with it: BOSS's probe, last 24 h.
            "p95_source": "probe", "window": WINDOW_WORD,
            "series": {"errors": errors, "latency": latency},
            "slo": {"target": DEFAULT_SLO, "burn": burn(uptime),
                    "budget_used_pct": budget_used(uptime)},
            "firing": fires.get(t["target_id"], []),
            "last_checked": t.get("last_checked"), "last_error": t.get("last_error"),
            "deployed_commit": (t.get("deployed_commit") or "")[:7] or None,
            "deployed_at": t.get("deployed_at"),
            "host": (hosts.get(str(t["target_id"])) or {}).get("address")
            or (hosts.get(str(t["target_id"])) or {}).get("name"),
            "host_href": (href(project_id, area="infra",
                               host=hosts[str(t["target_id"])]["host_id"])
                          if (hosts.get(str(t["target_id"])) or {}).get("host_id") else None),
            "href": project_href(project_id, tab="health", drawer=f"env:{t['target_id']}"),
        })
    return out


def _short(title: Any) -> str:
    from .titles import shorten

    return shorten(title, 5) or "Incident"


def rule_words(name: Any) -> str:
    """An alert rule's name in words: `HelpdeskErrorRate` / `error_rate_high` → "Error rate
    high"; ≤ 4 words (app_alerts.rule_name_words, the one rule)."""
    from ..app_alerts import rule_name_words

    return rule_name_words(name, 4)


def alert_name(summary: Any, rule: Any, kind: Any = None) -> str:
    """An alert's NAME on screen 12 — THE SAME NAME Observability and the parity component show
    (app_alerts.rule_label): the built-in rule's label, else its summary when that is a short
    name ("5xx on /api/tickets"), else the rule's name in words ("Error rate") — never a long
    sentence cut to fit ("1 rule group(s) are evaluating late…") nor `TicketsApi5xx`."""
    from ..app_alerts import rule_label

    return rule_label(kind, rule, summary)


def _quiet(at: Optional[datetime], now: datetime) -> str:
    """"quiet 3d": how long a rule has been calm (since it last cleared, or since it exists)."""
    if at is None:
        return "quiet"
    from .common import age

    return f"quiet {age(at, now)}"


def alerts(connection: Connection, project_id: str, *, now: Optional[datetime] = None,
           limit: int = 20) -> list[dict[str, Any]]:
    """THE ALERTS CARD (screen 12): one row per alert RULE of this project's apps — its name
    ("p95 latency", "5xx on /api/tickets"), severity and where it stands: firing since …,
    resolved at …, or ok · quiet 3d — then anything that fired without a rule (an external
    source). Firing first. BOSS's own platform rules and alerts (layer boss) are not the app's
    and live in Observability's BOSS view."""
    from .. import app_alerts as aa
    from .. import severity as sev

    now = now or datetime.now(timezone.utc)
    rules = connection.execute(text(
        "SELECT r.id, r.kind, r.alert, r.params, r.for_seconds, r.labels, r.annotations, "
        "r.created_at, r.last_state, t.app_name, t.environment, "
        "(SELECT row_to_json(x) FROM (SELECT a.id, a.state, a.active_at, a.fired_at, a.cleared_at "
        " FROM app_alerts a WHERE a.rule_id = r.id ORDER BY COALESCE(a.fired_at, a.active_at) DESC "
        " LIMIT 1) x) AS last, r.enabled "
        "FROM app_alert_rules r "
        "JOIN deployment_targets t ON t.id = COALESCE(r.subject_id, r.target_id) "
        "WHERE r.project_id = :p AND t.status = 'active' "
        "AND COALESCE(r.labels->>'layer', '') <> 'boss' "
        "AND COALESCE(r.labels->>'boss_meta', '') <> 'true' "
        "ORDER BY t.environment, r.created_at"), {"p": project_id}).mappings().all()
    # EVERY RULE the pack counts and the parity component lists, a switched-off one too (as
    # "off"), so "5 rules · 1 off" sits above five rows here and five in the component (12-17).
    envs = {str(r["environment"]) for r in rules}
    out: list[dict[str, Any]] = []
    for r in rules:
        labels = as_json(r["labels"], {}) or {}
        ann = as_json(r["annotations"], {}) or {}
        last = as_json(r["last"], {}) or {}
        name = alert_name(ann.get("summary"), r["alert"], r["kind"])
        if len(envs) > 1:
            name = f"{name} · {env_label(r['environment'])}"
        state, since = "ok", None
        if not r["enabled"]:
            state = "off"
        elif last and last.get("state") in ("firing", "pending"):
            state, since = "firing", last.get("fired_at") or last.get("active_at")
        elif last and last.get("cleared_at") and \
                str(last["cleared_at"]) >= (now - timedelta(days=1)).isoformat():
            state, since = "resolved", last.get("cleared_at")
        quiet_from = (datetime.fromisoformat(str(last["cleared_at"])) if last.get("cleared_at")
                      else r["created_at"])
        out.append({
            "id": str(last.get("id") or r["id"]), "rule_id": r["id"], "label": name,
            "condition": aa.condition_words(str(r["kind"]), as_json(r["params"], {}) or {},
                                            int(r["for_seconds"] or 0)),
            "detail": ann.get("description") or ann.get("summary") or name,
            "severity": _SEVERITY.get(sev.level(labels), "ticket"),
            "state": state, "pending": bool(last) and last.get("state") == "pending",
            "since": iso(since) if since else None,
            "quiet": _quiet(quiet_from if isinstance(quiet_from, datetime) else None, now)
            if state == "ok" else ("switched off" if state == "off" else None),
            "cleared_at": last.get("cleared_at"), "app": r["app_name"], "env": r["environment"],
            "href": href(project_id, area="observability", view="alerts",
                         alert=last["id"]) if last.get("id") else
            href(project_id, area="observability", view="alerts", rule=r["id"]),
        })
    # What fired without one of this project's rules (an external source, a removed rule).
    loose = connection.execute(text(
        "SELECT a.id, a.alertname, a.labels, a.annotations, a.state, a.active_at, a.fired_at, "
        "a.cleared_at, a.incident_id, t.app_name, t.environment, r.alert AS rule_name "
        "FROM app_alerts a "
        "LEFT JOIN deployment_targets t ON t.id = COALESCE(a.subject_id, a.target_id) "
        "LEFT JOIN app_alert_rules r ON r.id = a.rule_id "
        "WHERE a.project_id = :p AND COALESCE(a.labels->>'layer', '') <> 'boss' "
        "AND COALESCE(a.labels->>'boss_meta', '') <> 'true' "
        "AND r.id IS NULL "
        "AND (a.state IN ('pending', 'firing') OR a.active_at >= :since "
        "OR a.cleared_at >= :since) ORDER BY (a.state <> 'resolved') DESC, "
        "COALESCE(a.fired_at, a.active_at) DESC LIMIT :n"),
        {"p": project_id, "since": now - timedelta(days=7), "n": limit}).mappings().all()
    for r in loose:
        labels = as_json(r["labels"], {}) or {}
        ann = as_json(r["annotations"], {}) or {}
        firing = r["state"] != "resolved"
        out.append({
            # THE RULE'S NAME ("Error rate"), never a grouped summary sentence.
            "id": r["id"], "rule_id": None,
            "label": alert_name(ann.get("summary"), r["rule_name"] or r["alertname"]),
            "condition": None,
            "detail": ann.get("description") or ann.get("summary") or r["alertname"],
            "severity": _SEVERITY.get(sev.level(labels), "ticket"),
            "state": "firing" if firing else "resolved", "pending": r["state"] == "pending",
            "since": iso(r["fired_at"] or r["active_at"]) if firing else iso(r["cleared_at"]),
            "quiet": None, "cleared_at": iso(r["cleared_at"]),
            "app": r["app_name"], "env": r["environment"], "incident_id": r["incident_id"],
            "href": href(project_id, area="observability", view="alerts", alert=r["id"]),
        })
    order = {"firing": 0, "resolved": 1, "ok": 2, "off": 3}
    out.sort(key=lambda x: (order.get(x["state"], 3), -(datetime.fromisoformat(str(x["since"]))
                                                         .timestamp() if x.get("since") else 0)))
    return out[:limit]


#: The squad's lifecycle as a 4-station strip: Alert → Triage → Fix → Verify.
def _mini(lifecycle: str, outcome: Optional[str]) -> list[dict[str, str]]:
    done_to = {"proposed": 1, "claimed": 1, "in_progress": 2, "needs_you": 2,
               "verifying": 3, "resolved": 4, "dismissed": 4}.get(lifecycle, 1)
    names = ("Alert", "Triage", "Fix", "Verify")
    out = []
    for i, name in enumerate(names):
        if i < done_to:
            state = "done"
        elif i == done_to:
            state = "waiting" if lifecycle == "needs_you" else "now"
        else:
            state = "todo"
        out.append({"label": name, "state": state})
    return out


#: The squad's ledger questions (incident_squad.Q_*) and the node each answer draws.
_Q_NODE = {"What caused it?": "cause", "How do we stop the bleeding?": "fix",
           "How do we fix it for good?": "fix", "Did it recover?": "verify"}


def _node_words(chosen: Any, limit: int = 3) -> str:
    """A ledger answer as a node: ≤ 3 words, no option key syntax ("pool_exhausted" → "Pool
    exhausted")."""
    from .titles import shorten

    raw = str(chosen or "").replace("_", " ").strip()
    return shorten(raw, limit) or raw[:24]


def _flows(connection: Connection, project_id: str, items: list[dict[str, Any]]
           ) -> dict[str, list[dict[str, str]]]:
    """EACH INCIDENT AS ITS OWN STORY (screen 12): the signal that fired → the SRE squad →
    the root cause it settled on → the fix it applied → Recovered (or "Approve fix" when the fix
    waits on a person, or Verify while it is checked) — from the squad's own ledger
    (agent_act_decisions) and the alert that opened it. An item with no ledger yet keeps the
    generic Alert → Triage → Fix → Verify strip."""
    ids = [str(i["id"]) for i in items]
    if not ids:
        return {}
    answers: dict[str, dict[str, str]] = {}
    for r in connection.execute(text(
            "SELECT work_item_id, question, chosen, outcome FROM agent_act_decisions "
            "WHERE project_id = :p AND work_item_id = ANY(:ids) ORDER BY created_at"),
            {"p": project_id, "ids": ids}).mappings():
        node = _Q_NODE.get(str(r["question"]))
        if node:
            answers.setdefault(str(r["work_item_id"]), {})[node] = str(r["chosen"])
            if node == "verify":
                answers[str(r["work_item_id"])]["verified"] = str(r["outcome"] or "")
    signals: dict[str, str] = {}
    for r in connection.execute(text(
            "SELECT l.work_item_id, a.alertname, a.annotations, a.value, a.unit, a.rule_kind "
            "FROM work_item_links l JOIN app_alerts a ON a.incident_id = l.ref "
            "WHERE l.work_item_id = ANY(:ids) AND l.kind = 'incident' "
            "ORDER BY COALESCE(a.fired_at, a.active_at) DESC"), {"ids": ids}).mappings():
        if str(r["work_item_id"]) in signals:
            continue
        ann = as_json(r["annotations"], {}) or {}
        if r["rule_kind"] == "latency_p95" and r["value"] is not None:
            v = float(r["value"])
            signals[str(r["work_item_id"])] = (f"p95 {v / 1000:.1f}s" if v >= 1000
                                               else f"p95 {int(v)}ms")
            continue
        signals[str(r["work_item_id"])] = alert_name(ann.get("summary"), r["alertname"])
    # WHAT IT LEARNED FROM BEFORE: the lesson the squad applied (its `reused` edge in the KG),
    # drawn as its own node ("INC-1 lesson") between the cause and the fix.
    lessons: dict[str, dict[str, str]] = {}
    for r in connection.execute(text(
            "SELECT s.attrs->>'work_item_id' AS wid, n.id, n.name, "
            "COALESCE(n.attrs->>'incident_ref', 'a past incident') AS ref FROM kg_edges e "
            "JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes n ON n.id = e.dst_id "
            "WHERE e.project_id = :p AND e.kind = 'reused' AND s.kind = 'incident' "
            "AND s.attrs->>'work_item_id' = ANY(:ids) ORDER BY e.recorded_at"),
            {"p": project_id, "ids": ids}).mappings():
        lessons[str(r["wid"])] = {"id": str(r["id"]), "name": str(r["name"] or ""),
                                  "ref": str(r["ref"])}
    # THE REAL TIMES (when it fired, when the mitigation went live, when recovery was
    # verified), from the incident row — never when a decision about them was taken.
    times: dict[str, dict[str, Any]] = {}
    for r in connection.execute(text(
            "SELECT l.work_item_id, i.first_seen, i.mitigated_at, i.resolved_at "
            "FROM work_item_links l JOIN incidents i ON i.id = l.ref "
            "WHERE l.work_item_id = ANY(:ids) AND l.kind = 'incident'"), {"ids": ids}).mappings():
        times.setdefault(str(r["work_item_id"]), dict(r))
    out: dict[str, list[dict[str, str]]] = {}
    for it in items:
        wid = str(it["id"])
        got = answers.get(wid)
        if not got:
            continue
        life = str(it.get("lifecycle") or "proposed")
        signal = signals.get(wid)
        # The card already names the alert ("5xx on /api/tickets"): its node just says Alert.
        if not signal or signal.lower() == str(it.get("title") or "").strip().lower():
            signal = "Alert"
        nodes = [{"label": words(signal, 3), "state": "done", "type": "signal"},
                 {"label": "SRE squad", "state": "done", "type": "agent"}]
        if got.get("cause"):
            nodes.append({"label": _node_words(got["cause"]), "state": "done", "type": "cause"})
        lesson = lessons.get(wid)
        if lesson:
            label = f"{lesson['ref']} lesson" if lesson["ref"].startswith("INC-") else "Lesson"
            nodes.append({"label": label, "state": "done", "type": "learning",
                          "detail": f"Applied learning: {lesson['name']} from {lesson['ref']}",
                          "node_id": lesson["id"]})
        if life == "needs_you":
            nodes.append({"label": "Approve fix", "state": "waiting", "type": "decision"})
            nodes.append({"label": "Recovered", "state": "todo"})
        else:
            if got.get("fix"):
                nodes.append({"label": _node_words(got["fix"]), "state": "done", "type": "fix"})
            resolved = life in ("resolved", "dismissed")
            nodes.append({"label": "Recovered" if resolved else "Verify",
                          "state": "done" if resolved else "now"})
        when = times.get(wid) or {}
        stamp = {"signal": when.get("first_seen"), "fix": when.get("mitigated_at")}
        for n in nodes:
            at = (when.get("resolved_at") if n["label"] == "Recovered" and n["state"] == "done"
                  else stamp.get(str(n.get("type") or "")))
            if at is not None:
                n["at"] = iso(at)
        out[wid] = nodes
    return out


#: An incident's line under its name (screen 12: "Today 09:12 · 14 min · resolved by the squad").
_OUTCOME_WORDS = {"resolved": "resolved by the squad", "needs_you": "fix needs your approval",
                  "running": "the squad is on it"}


def incidents(connection: Connection, project_id: str, *, limit: int = 10
              ) -> list[dict[str, Any]]:
    from ..incident_squad import INCIDENT_CLASSES

    rows = connection.execute(text(
        "SELECT w.id, w.title, w.lifecycle, w.outcome, w.app, w.created_at, "
        "COALESCE((SELECT i.resolved_at FROM work_item_links l JOIN incidents i ON i.id = l.ref "
        "  WHERE l.work_item_id = w.id AND l.kind = 'incident' LIMIT 1), w.resolved_at, "
        "  w.updated_at) AS updated_at, "
        "(SELECT l.ref FROM work_item_links l WHERE l.work_item_id = w.id AND l.kind = 'incident' "
        " LIMIT 1) AS incident_id FROM work_items w WHERE w.project_id = :p AND w.ambient "
        "AND w.task_class = ANY(:classes) ORDER BY (w.lifecycle IN ('resolved', 'dismissed')), "
        "w.updated_at DESC LIMIT :n"),
        {"p": project_id, "classes": list(INCIDENT_CLASSES), "n": limit}).mappings().all()
    flows = _flows(connection, project_id, [dict(r) for r in rows])
    out = []
    covered: set[str] = set()
    for r in rows:
        life = str(r["lifecycle"] or "proposed")
        state = ("resolved" if life in ("resolved", "dismissed") else
                 "needs_you" if life == "needs_you" else "running")
        if r["incident_id"] and state != "resolved":
            # A RESOLVED squad item does not cover an incident that is still open (HelpDesk AI
            # 2026-11-07: "resolved by the squad" here, "the incident is still open" in
            # Observability): that incident is listed below with why it is open.
            covered.add(str(r["incident_id"]))
        ended = r["updated_at"] if state == "resolved" else None
        out.append({"id": r["id"], "label": _short(r["title"]), "detail": r["title"],
                    "state": state, "app": r["app"], "since": iso(r["created_at"]),
                    "ended_at": iso(ended),
                    "duration_s": int((ended - r["created_at"]).total_seconds())
                    if ended is not None and r["created_at"] is not None else None,
                    "outcome_word": _OUTCOME_WORDS.get(state),
                    "incident_id": r["incident_id"],
                    "graph_href": href(project_id, area="agent_ops", item=r["id"]),
                    "mini": flows.get(str(r["id"])) or _mini(life, r["outcome"])})
    open_rows = connection.execute(text(
        "SELECT id, title, first_seen FROM incidents WHERE project_id = :p "
        "AND status NOT IN ('resolved', 'dismissed') ORDER BY last_seen DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    from ..app_alerts import why_open

    for r in open_rows:
        if str(r["id"]) in covered:
            continue
        # THE SAME WORDS AS OBSERVABILITY (app_alerts.why_open): why it is still open and who
        # acts; "nobody has picked it up" only while an alert in it still fires.
        why = why_open(connection, project_id, str(r["id"]))
        state = "needs_you" if not why or why["who"] == "person" else "running"
        out.append({"id": r["id"], "label": _short(r["title"]), "detail": r["title"],
                    "state": state, "app": None, "since": iso(r["first_seen"]),
                    "ended_at": None, "duration_s": None,
                    "outcome_word": f"recovered; {why['word']}" if why else
                    "nobody has picked it up",
                    "closes_at": why.get("closes_at") if why else None,
                    "incident_id": r["id"],
                    "graph_href": href(project_id, area="observability", inc=r["id"]),
                    "mini": _mini("verifying" if state == "running" else state, None)})
    # Newest first (screen 12: this morning's 5xx above Wednesday's slow list).
    out.sort(key=lambda i: str(i.get("since") or ""), reverse=True)
    return out[:limit]


#: What each kind of rule watches, as the pack's chips (screen 12: Uptime · 5xx · Latency …).
_PACK_WORDS = {"error_rate": "5xx", "latency_p95": "Latency", "health_check": "Uptime",
               "no_traffic": "Traffic", "deploy_health": "Deploys"}
_PACK_HINTS = (("disk", "Disk"), ("filesystem", "Disk"), ("pg_", "DB conns"), ("db_conn", "DB conns"),
               ("connections", "DB conns"), ("tls", "TLS"), ("cert", "TLS"), ("ssl", "TLS"),
               ("5xx", "5xx"), ("latency", "Latency"), ("probe", "Uptime"), ("memory", "Memory"),
               ("cpu", "CPU"), ("queue", "Queue"))


def _pack_word(kind: str, alert: str, expr: str) -> str:
    if kind in _PACK_WORDS:
        return _PACK_WORDS[kind]
    hay = f"{alert} {expr}".lower()
    for needle, word in _PACK_HINTS:
        if needle in hay:
            return word
    return rule_words(alert)


def monitoring_pack(connection: Connection, project_id: str) -> dict[str, Any]:
    """Beacon's pack: how many rules, where they live (the repo's boss.deploy.yaml or BOSS),
    what they watch (one chip per kind) and, for a repo pack, the file on GitHub."""
    # THE SAME RULES the Alerts card and Observability's rule list show: this project's app
    # rules on its active targets (BOSS's own layer-boss rules are not the app's). Switched-off
    # rules are counted apart, so "6 rules · 1 off" agrees with the list below it.
    every = connection.execute(text(
        "SELECT r.kind, r.alert, r.expr, r.source, r.enabled FROM app_alert_rules r "
        "JOIN deployment_targets t ON t.id = COALESCE(r.subject_id, r.target_id) "
        "WHERE r.project_id = :p AND t.status = 'active' "
        "AND COALESCE(r.labels->>'layer', '') <> 'boss' "
        "AND COALESCE(r.labels->>'boss_meta', '') <> 'true' ORDER BY r.created_at"),
        {"p": project_id}).mappings().all()
    rows = [r for r in every if r["enabled"]]
    off = len(every) - len(rows)
    # THE COUNT IS EVERY RULE, the off ones included and said: "5 rules · 1 off" above the five
    # rules the Alerts card and the parity component list (12-17 / PX-35).
    rules = len(every)
    repo = sum(1 for r in rows if r["source"] == "yaml")
    managed = "none" if not rules else "repo" if repo else "boss"
    kinds: list[str] = []
    for r in rows:
        w = _pack_word(str(r["kind"]), str(r["alert"] or ""), str(r["expr"] or ""))
        if w not in kinds:
            kinds.append(w)
    file = None
    if repo:
        got = connection.execute(text(
            "SELECT s.repository, s.commit_sha, s.manifest->>'manifest_path' AS path "
            "FROM stack_deployments s JOIN deployment_targets t ON t.id = s.target_id "
            "WHERE t.project_id = :p AND s.status = 'healthy' ORDER BY s.created_at DESC LIMIT 1"),
            {"p": project_id}).mappings().first()
        if got and got["repository"]:
            path = got["path"] or "boss.deploy.yaml"
            file = {"path": path, "external_url":
                    f"https://github.com/{got['repository']}/blob/{got['commit_sha'] or 'HEAD'}/{path}"}
    return {"rules": rules, "off": off, "managed_in": managed, "kinds": kinds[:8], "file": file,
            # Observability › Alerts: every app's rules and what fired (no `rules` view exists).
            "href": href(project_id, area="observability", view="alerts")}


def links(connection: Connection, project_id: str, apps: list[str]) -> dict[str, Any]:
    from ..observability_stack import endpoint_state

    try:
        with connection.begin_nested():
            grafana = endpoint_state(connection, project_id, "grafana")
    except Exception:  # noqa: BLE001 — a link is a help, never a failed tab
        grafana = {"state": "unknown", "url": None, "reason": None}
    return {"grafana": grafana.get("url") if grafana.get("state") == "answering" else None,
            "grafana_state": grafana.get("state"), "grafana_reason": grafana.get("reason") or None,
            # The deploy log (Infra › Deploys); Observability has no logs view of its own.
            "logs": href(project_id, area="infra", view="deploys"),
            "observability": href(project_id, area="observability",
                                  app=apps[0] if len(apps) == 1 else None)}


def view(connection: Connection, project_id: str, *, now: Optional[datetime] = None
         ) -> dict[str, Any]:
    envs = environments(connection, project_id, now=now)
    pack = monitoring_pack(connection, project_id)
    out = {"environments": envs, "alerts": alerts(connection, project_id, now=now),
           "incidents": incidents(connection, project_id), "monitoring_pack": pack,
           # Observability opens an app by its deploy target id (`app=<target id>`), never its name.
           "links": links(connection, project_id, sorted({e["target_id"] for e in envs}))}
    if not envs:
        out["empty"] = {"label": "Nothing deployed yet",
                        "detail": "Health appears after the first deploy: Beacon adds a "
                                  "monitoring pack to the app's boss.deploy.yaml when it ships.",
                        "href": project_href(project_id, tab="deployments")}
    elif not pack["rules"]:
        out["empty"] = {"label": "No alert rules yet",
                        "detail": "Beacon writes the monitoring pack on the next deploy; until "
                                  "then BOSS only checks that the app answers.",
                        "href": pack["href"]}
    return out


def rollup(envs: list[dict[str, Any]]) -> str:
    """The worst health across environments (for the header dot and project cards)."""
    return worst(envs)[0]


def worst(envs: list[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """(the worst health across environments, its reason with the environment named)."""
    order = ("down", "degraded", "healthy")
    for word in order:
        hit = next((e for e in envs if e.get("health") == word), None)
        if hit:
            why = hit.get("health_reason")
            name = hit.get("label") or env_label(hit.get("key") or hit.get("environment"))
            return word, (f"{name}: {why}" if why and len(envs) > 1 else why)
    return "unknown", None


__all__ = ["DEFAULT_SLO", "WINDOW_WORD", "alerts", "rule_words", "burn", "env_label",
           "environments", "firing_alerts", "health_word", "incidents", "links",
           "monitoring_pack", "rollup", "state_of", "target_states", "view", "worst"]
