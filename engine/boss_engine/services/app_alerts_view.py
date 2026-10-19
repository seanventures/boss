"""What Observability shows about alerts: one app's Alerts tab, and every app's alerts.

Read-only over app_alerts' records and the recorded Grafana check — never a live probe, never a
model call. Every fired alert carries its PromQL `expr`, its value at breach, a Grafana Explore
link for the breach window (only when BOSS checked that Grafana answers; otherwise the reason),
its runbook and the agent's triage in the agent's words.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..ports import sql
from . import app_alerts as aa
from . import severity as sev

#: History windows the tab offers.
SINCE = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
#: Firing, and nobody has it: not acknowledged (or the ack expired), not silenced, not held back.
NEEDS_ATTENTION_SQL = (
    "(a.acked_at IS NULL OR a.ack_expires_at <= now()) AND a.silenced_by IS NULL "
    "AND a.inhibited_by IS NULL "
    "AND (a.suppressed_by IS NULL OR a.suppressed_by IN ('[]'::jsonb, 'null'::jsonb))")

_STATE_WORDS = {"firing": "Firing", "pending": "Pending", "ok": "OK", "no_data": "No data",
                "too_little": "Too little traffic", "off": "Off", "silenced": "Silenced",
                "inhibited": "Held back", "error": "Can't evaluate", None: "Not evaluated yet"}
_STATE_TONES = {"firing": "red", "pending": "amber", "inhibited": "amber", "silenced": "grey",
                "error": "amber", "no_data": "grey", "too_little": "grey", "off": "grey",
                "ok": "green", None: "grey"}
_STATE_TIPS = {
    "no_data": "The app exports none of the metrics this rule reads",
    "too_little": "Too few requests in the window to judge",
    "inhibited": "Held back while the app's health or deploy alert fires",
    "error": None, None: "BOSS evaluates rules on its monitoring sweep",
}
_CLEARED_WORDS = {"source_resolved": "Resolved by its source",
                  "source_quiet": "Source went quiet for 24 hours",
                  "recovered": "Recovered", "no_data": "Data stopped",
                  "too_little": "Traffic too low to judge", "rule_off": "Rule switched off",
                  "incident_closed": "Incident closed", "error": "Could not evaluate"}
_SOURCE_WORDS = {"yaml": "from boss.deploy.yaml", "builtin": "BOSS default",
                 "console": "changed in the console"}
_EVAL_WORDS = {"boss": "BOSS's own monitoring", "prometheus": "Prometheus",
               "alertmanager": "Alertmanager", "grafana": "Grafana", "sentry": "Sentry",
               "datadog": "Datadog", "cloudwatch": "CloudWatch", "pagerduty_v2": "PagerDuty event",
               "generic": "An event", "heartbeat": "Heartbeat"}
_SUPPRESS_WORDS = {"silence": "Silenced", "maintenance": "In a maintenance window",
                   "deploy_hold": "Held for the agent: a deploy just finished",
                   "inhibit": "Held back by a firing cause"}


def _json(v: Any, default: Any = None) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (v or None)


def _person(connection, pid: Optional[str]) -> Optional[str]:
    if not pid:
        return None
    from .incident_response import _person as person

    return person(connection, pid)


def value_word(value: Optional[float], unit: str) -> str:
    if value is None:
        return "—"
    v = round(float(value), 1)
    text = aa.num(v)
    if unit == "%":
        return f"{text}%"
    if unit == "ms":
        return f"{text} ms" if v < 1000 else f"{aa.num(round(v / 1000.0, 2))} s"
    return f"{text} {unit}".strip()


def _runbook(url: str, target: dict) -> dict[str, Any]:
    """A runbook is BOSS's own (boss:runbook/<name>), a markdown page kept with the app (a path
    in its repo), or a URL."""
    url = (url or "").strip()
    if url.startswith(aa.BUILTIN_RUNBOOK):
        name = url[len(aa.BUILTIN_RUNBOOK):]
        book = aa.RUNBOOKS.get(name)
        return {"url": url, "kind": "builtin", "title": (book or {}).get("title") or name,
                "markdown": (book or {}).get("markdown"), "href": None}
    if url.startswith(("http://", "https://")):
        return {"url": url, "kind": "link", "title": url, "markdown": None, "href": url}
    if url:
        repo = str(target.get("repo") or "").strip()
        href = None
        if repo and "/" in repo:
            from ..identity.github_urls import web_base

            href = f"{web_base()}/{repo}/blob/HEAD/{url.lstrip('/')}"
        return {"url": url, "kind": "repo", "title": url, "markdown": None, "href": href}
    return {"url": "", "kind": "none", "title": "No runbook", "markdown": None, "href": None}


def _grafana(connection, project_id: str) -> dict[str, Any]:
    from .observability_stack import endpoint_state

    return endpoint_state(connection, project_id, "grafana")


def _explore(grafana: dict, alert: dict) -> tuple[Optional[str], Optional[str]]:
    """Grafana Explore for the breach window (10 minutes before it fired to when it cleared, or
    now), or None and why."""
    from .observability_stack import explore_url

    if alert.get("source_kind") == "signal" or not alert.get("expr"):
        return None, None
    if grafana.get("state") != "answering":
        return None, grafana.get("reason") or "Grafana isn't answering"
    start = (alert.get("fired_at") or alert.get("active_at")) - timedelta(minutes=10)
    end = alert.get("cleared_at") or datetime.now(timezone.utc)
    return explore_url(grafana.get("public_url") or grafana["base_url"], alert["expr"], int(start.timestamp() * 1000),
                       int(end.timestamp() * 1000)), None


def _triage_wire(alert: dict) -> dict[str, Any]:
    t = alert.get("triage") or {}
    st = alert.get("triage_state")
    if st == "no_model":
        return {"state": "no_model", "word": aa.NO_MODEL_WORD, "reason": None, "question": None,
                "outcome": None, "by": None, "at": t.get("at"), "confidence": None,
                "agent": None, "model": None}
    if st == "off":
        return {"state": "off", "word": "Triage off for this rule", "reason": None,
                "question": None, "outcome": None, "by": None, "at": None, "confidence": None,
                "agent": None, "model": None}
    if st in ("pending", "skipped") or not t:
        held = alert.get("inhibited_by") or alert.get("silenced_by")
        word = ("Not triaged: held back" if alert.get("inhibited_by") else
                "Not triaged: silenced" if alert.get("silenced_by") else
                "Waiting for the agent" if st == "pending" and alert.get("state") == "firing"
                else "Not triaged")
        return {"state": "held" if held else st or "none", "word": word, "reason": None,
                "question": None, "outcome": None, "by": None, "at": None, "confidence": None,
                "agent": None, "model": None}
    outcome = t.get("outcome")
    word = {"opened": "Opened an incident", "joined": "Joined the incident",
            "split": "Opened its own incident", "noise": "Noise", "asked": "Asks you",
            "page": "Page now", "storm": t.get("word") or "Storm"}.get(
        outcome or "", t.get("word") or "Triaged")
    by = t.get("by") or {}
    return {"state": "done", "word": word, "outcome": outcome, "reason": t.get("reason") or None,
            "question": t.get("question") or None,
            "by": by.get("word") or by.get("model") or by.get("backend") or None,
            # The agent that decided (role), apart from the model it thought with.
            "agent": ({"role_key": by["role_key"], "role": by.get("role") or by["role_key"]}
                      if by.get("role_key") else None),
            "model": by.get("model") or None,
            "at": t.get("at"), "confidence": t.get("confidence")}


def _minutes(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    m = int(round(seconds / 60))
    if m < 1:
        return f"{int(seconds)} s"
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


def duration_s(a: dict, now: Optional[datetime] = None) -> Optional[int]:
    """How long it has been (or was) open: fired (else active) to cleared (else now). A pending
    alert counts from when it went pending: an alert that fired, cleared and is pending again
    keeps its first `fired_at`, and "pending 39 min" from it was a time it never waited."""
    start = (a.get("active_at") if a.get("state") == "pending" else None) \
        or a.get("fired_at") or a.get("active_at")
    if not start:
        return None
    end = a.get("cleared_at") or now or datetime.now(timezone.utc)
    return max(0, int((end - start).total_seconds()))


def alert_status(a: dict, acked_by: Optional[str], now: Optional[datetime] = None
                 ) -> dict[str, Any]:
    """THE status chip, one answer for every row: is it still open, and who has it?

    firing (red: needs attention) · acknowledged (amber, says who) · pending (grey: waiting out
    `for`) · resolved (green, with time and duration) · silenced / maintenance (muted: open, but
    nobody is told)."""
    now = now or datetime.now(timezone.utc)
    dur = duration_s(a, now)
    if a["state"] == "resolved":
        at = a.get("cleared_at")
        return {"key": "resolved", "tone": "green", "word": "Resolved",
                "tip": (f"Resolved at {at.strftime('%H:%M')} UTC" if at else "Resolved")
                       + (f", open {_minutes(dur)}" if dur is not None else "")}
    kinds = {x.get("kind") for x in (_json(a.get("suppressed_by"), []) or [])}
    if "maintenance" in kinds:
        return {"key": "maintenance", "tone": "muted", "word": "Maintenance",
                "tip": "Open, in a maintenance window: nobody is told until it ends"}
    if a.get("silenced_by") or "silence" in kinds:
        return {"key": "silenced", "tone": "muted", "word": "Silenced",
                "tip": "Open but silenced: it is still evaluated, nobody is told"}
    if a.get("inhibited_by") or "inhibit" in kinds:
        return {"key": "silenced", "tone": "muted", "word": "Held back",
                "tip": "Open, held back while its cause (health or deploy) fires"}
    if a["state"] == "pending":
        return {"key": "pending", "tone": "grey", "word": "Pending",
                "tip": "Breached, not firing yet: it must stay true for its `for`"}
    if acked_by:
        return {"key": "acknowledged", "tone": "amber", "word": f"Acknowledged · {acked_by}",
                "tip": f"{acked_by} is on it: repeats stopped, it is still firing"}
    return {"key": "firing", "tone": "red", "word": "Firing",
            "tip": "Firing and nobody has acknowledged it: needs attention"
                   + (f" (open {_minutes(dur)})" if dur is not None else "")}


def _alert_wire(connection, a: dict, *, target: dict, grafana: dict,
                incidents: dict[str, dict], names: dict[str, str]) -> dict[str, Any]:
    labels = _json(a.get("labels"), {}) or {}
    ann = _json(a.get("annotations"), {}) or {}
    rel = _json(a.get("release"), None) or {}
    a = {**a, "labels": labels, "annotations": ann, "triage": _json(a.get("triage"), None)}
    explore, explore_why = _explore(grafana, a)
    inc = incidents.get(a.get("incident_id") or "")
    kind = a["rule_kind"]
    label = aa.BUILTIN.get(kind, {}).get("label") or (
        "Reported" if a["source_kind"] == "signal" else a["alertname"])
    unit = a.get("unit") or ""
    acked_now = bool(a.get("acked_at") and (a.get("ack_expires_at") is None
                                           or a["ack_expires_at"] > datetime.now(timezone.utc)))
    acked_by = (_person(connection, a["acked_by"]) or a["acked_by"]) if acked_now else None
    status = alert_status(a, acked_by)
    summary = ann.get("summary") or a["alertname"]
    if a["source_kind"] == "signal":
        summary = f"{names.get(a.get('reporter_target_id') or '', 'An app')}: {summary}"
    return {
        "id": a["id"], "target_id": a["target_id"], "app": target.get("app_name"),
        "env": target.get("environment"), "rule_id": a.get("rule_id"), "kind": kind,
        "label": label, "alertname": a["alertname"], "summary": summary,
        "description": ann.get("description") or None,
        "severity": labels.get("severity") or "warning", "sev": sev.level(labels),
        "status": status, "open": a["state"] != "resolved",
        "needs_attention": status["key"] == "firing", "duration_s": duration_s(a),
        "state": a["state"],
        "state_word": {"firing": "Firing", "pending": "Pending",
                       "resolved": "Resolved"}[a["state"]],
        "active_at": _iso(a["active_at"]), "fired_at": _iso(a.get("fired_at")),
        "cleared_at": _iso(a.get("cleared_at")),
        "cleared_word": _CLEARED_WORDS.get(a.get("cleared_reason") or "", None),
        "fire_count": int(a.get("fire_count") or 1),
        "value": a.get("value"), "value_word": value_word(a.get("value"), unit),
        "threshold": a.get("threshold"),
        "threshold_word": value_word(a.get("threshold"), unit) if a.get(
            "threshold") is not None else None,
        "expr": a.get("expr") or None, "evaluated_by": _EVAL_WORDS.get(a.get("eval_source") or "",
                                                                        None),
        "release": ({"sha7": str(rel.get("commit_sha") or "")[:7] or None, "ref": rel.get("ref"),
                     "deployed_at": rel.get("deployed_at")} if rel else None),
        "incident": ({"id": inc["id"], "title": inc["title"], "status": inc["status"],
                      "word": inc["status"].capitalize()} if inc else None),
        "held_back": ({"by": "inhibited", "word": "Held back: the app's health or deploy alert"
                       " is firing"} if a.get("inhibited_by") else
                      {"by": "silenced", "word": "Silenced"} if a.get("silenced_by") else None),
        "triage": _triage_wire(a),
        "response": ({"run_id": a.get("response_run_id"), "word": a.get("response_word")}
                     if a.get("response_word") else None),
        "runbook": _runbook(ann.get("runbook_url") or "", target),
        "explore_url": explore, "explore_reason": explore_why,
        "source_kind": a["source_kind"],
        "reporter": ({"target_id": a.get("reporter_target_id"),
                      "app": names.get(a.get("reporter_target_id") or "")}
                     if a["source_kind"] == "signal" else None),
        "backfilled": bool(a.get("backfilled")),
        "subject_id": a.get("subject_id") or a.get("target_id"),
        "service_id": a.get("service_id"),
        "source": _EVAL_WORDS.get(a.get("eval_source") or "", a.get("eval_source")),
        "urgency": a.get("urgency") or "high",
        "flapping": bool(a.get("flapping")),
        "flap_score": a.get("flap_score"),
        "flap_tuning": ((a.get("triage") or {}).get("flap_tuning") or None),
        "acked": ({"by": acked_by, "at": _iso(a["acked_at"]),
                   "until": _iso(a.get("ack_expires_at"))} if acked_now else None),
        "no_data_since": _iso(a.get("no_data_since")),
        "keep_firing_since": _iso(a.get("missing_since")),
        "suppressed": [{"kind": x.get("kind"), "id": x.get("id"), "until": x.get("until"),
                        "word": _SUPPRESS_WORDS.get(x.get("kind") or "", x.get("kind")),
                        "reason": x.get("reason")}
                       for x in (_json(a.get("suppressed_by"), []) or [])],
        "links": _json(a.get("links"), []) or [],
        "notified_at": _iso(a.get("notified_at")),
    }


def _targets(connection, project_id: str, ids: Optional[list[str]] = None) -> dict[str, dict]:
    q = "SELECT * FROM deployment_targets WHERE project_id = :p"
    params: dict[str, Any] = {"p": project_id}
    if ids is not None:
        q += " AND id = ANY(:ids)"
        params["ids"] = ids
    return {r["id"]: dict(r) for r in connection.execute(sql(q), params).mappings().all()}


def _incidents(connection, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    return {r["id"]: dict(r) for r in connection.execute(sql(
        "SELECT id, title, status FROM incidents WHERE id = ANY(:i)"),
        {"i": ids}).mappings().all()}


def incident_states(connection, project_id: str, *, subject_id: Optional[str] = None,
                    incident_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """THE ONE ANSWER to "is this incident's problem happening now?", read from its alerts:
    every card, list and badge about an open incident says what this says. An incident can be
    open while nothing fires (it recovered, and a response or a person still has it): that is
    said as such, never as "requests failing"."""
    from .incident_response import _response_in_flight

    q = ("SELECT i.id, i.title, i.status, i.lifecycle, i.first_seen, i.acked_by, i.urgency, "
         " i.finalize_ctx, i.severity, "
         " array_agg(a.labels->>'severity') FILTER (WHERE a.id IS NOT NULL) AS sevs, "
         " count(a.id) FILTER (WHERE a.state = 'firing' AND a.inhibited_by IS NULL) AS firing, "
         " count(a.id) FILTER (WHERE a.state = 'pending') AS pending, "
         " count(a.id) AS alerts, max(a.cleared_at) AS last_cleared, "
         " min(COALESCE(a.subject_id, a.target_id)) AS subject_id "
         "FROM incidents i LEFT JOIN app_alerts a ON a.incident_id = i.id "
         "WHERE i.project_id = :p AND i.status NOT IN ('resolved', 'dismissed')")
    params: dict[str, Any] = {"p": project_id}
    if incident_ids is not None:
        q += " AND i.id = ANY(:ids)"
        params["ids"] = list(incident_ids)
    q += " GROUP BY i.id"
    out = []
    for r in connection.execute(sql(q), params).mappings().all():
        if subject_id and r["subject_id"] != subject_id:
            continue
        firing = int(r["firing"] or 0)
        run = _response_in_flight(connection, project_id, r["id"])
        run_status = connection.execute(sql("SELECT status FROM workflow_runs WHERE id = :i"),
                                        {"i": run}).scalar() if run else None
        if firing:
            word = f"Firing: {firing} alert{'s' if firing != 1 else ''}"
        elif int(r["pending"] or 0):
            word = "Pending: an alert is waiting out its `for`"
        elif int(r["alerts"] or 0):
            # The clock is the viewer's: the time travels as `recovered_at`, never baked into
            # the words in UTC beside a list that reads in local time (22:46 next to 15:46).
            word = "Recovered" + (
                "; a response is waiting on you" if run_status == "waiting" else
                "; a response is still running" if run else "; the incident is still open")
        else:
            word = "Reported; no alert of BOSS's is firing"
        level = sev.worst(r["sevs"] or []) or sev.level(r["severity"])
        acked_by = (_person(connection, r["acked_by"]) or r["acked_by"]) if r["acked_by"] \
            else None
        if firing and acked_by:
            chip = {"key": "acknowledged", "tone": "amber", "word": f"Acknowledged · {acked_by}",
                    "tip": f"{acked_by} is on it; its alerts still fire"}
        elif firing:
            chip = {"key": "firing", "tone": "red", "word": "Firing",
                    "tip": "Still happening and nobody has acknowledged it"}
        elif int(r["pending"] or 0):
            chip = {"key": "pending", "tone": "grey", "word": "Pending",
                    "tip": "An alert is breached, waiting out its `for`"}
        else:
            chip = {"key": "recovered", "tone": "green", "word": "Recovered",
                    "tip": "Nothing fires now; the incident is still open until it is closed"}
        out.append({"id": r["id"], "title": r["title"], "status": r["status"],
                    "sev": level, "status_chip": chip, "acked_by": acked_by,
                    "lifecycle": r["lifecycle"], "since": _iso(r["first_seen"]),
                    "firing": firing, "happening": bool(firing),
                    "subject_id": r["subject_id"], "urgency": r["urgency"],
                    "acked": bool(r["acked_by"]), "response_run_id": run,
                    "response_status": run_status, "word": word,
                    "recovered_at": (_iso(r["last_cleared"]) if not firing and not int(
                        r["pending"] or 0) and int(r["alerts"] or 0) else None)})
    return sorted(out, key=lambda x: (not x["happening"], x["since"] or ""))


def _subjects(connection, project_id: str, ids: list[str]) -> dict[str, dict]:
    """Targets and monitored services by the id alerts carry, each with app_name/environment."""
    out = _targets(connection, project_id, ids)
    rest = [i for i in ids if i not in out]
    if rest:
        for r in connection.execute(sql(
                "SELECT id, name, environment, deployment_target_id FROM monitored_services "
                "WHERE project_id = :p AND id = ANY(:i)"), {"p": project_id, "i": rest}
        ).mappings().all():
            out[r["id"]] = {"id": r["id"], "app_name": r["name"],
                            "environment": r["environment"], "subject_kind": "service"}
    return out


def alerts(connection, project_id: str, *, target_id: Optional[str] = None,
           state: str = "all", since: str = "7d", severity: Optional[str] = None,
           limit: int = 100, source: Optional[str] = None, flapping: Optional[bool] = None,
           acked: Optional[bool] = None, attention: bool = False) -> dict[str, Any]:
    """Alerts, newest first. state firing (open now: pending + firing) | resolved | all.
    Filters: an app or service (target_id: either id), severity, source kind (rule, external,
    synthetic, heartbeat, signal), flapping, acked."""
    now = datetime.now(timezone.utc)
    where = ["a.project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "n": max(1, min(int(limit), 500))}
    if target_id:
        where.append("a.subject_id = :t")
        params["t"] = target_id
    if source in ("rule", "external", "synthetic", "heartbeat", "signal"):
        where.append("a.source_kind = :src")
        params["src"] = source
    if flapping is not None:
        where.append("a.flapping = :fl")
        params["fl"] = bool(flapping)
    if acked is not None:
        where.append("(a.acked_at IS NOT NULL AND (a.ack_expires_at IS NULL OR "
                     "a.ack_expires_at > now()))" if acked else
                     "(a.acked_at IS NULL OR a.ack_expires_at <= now())")
    if attention:
        state = "firing"
        where.append("a.state = 'firing' AND " + NEEDS_ATTENTION_SQL)
    if state == "firing":
        where.append("a.state IN ('pending', 'firing')")
    else:
        if state == "resolved":
            where.append("a.state = 'resolved'")
        params["since"] = now - SINCE.get(since, SINCE["7d"])
        where.append("(a.state IN ('pending', 'firing') OR a.active_at >= :since "
                     "OR a.cleared_at >= :since)")
    if severity and sev.accepted(severity):
        words = [w for w in sev.ACCEPTED + tuple(sev._ALIASES) if sev.level(w) ==
                 sev.level(severity)]
        where.append("COALESCE(lower(a.labels->>'severity'), 'warning') = ANY(:sevs)")
        params["sevs"] = sorted(set(words))
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT a.* FROM app_alerts a WHERE " + " AND ".join(where)
        + " ORDER BY (a.state <> 'resolved') DESC, COALESCE(a.fired_at, a.active_at) DESC "
          "LIMIT :n"), params).mappings().all()]
    tids = sorted({r["subject_id"] or r["target_id"] for r in rows}
                  | {r["reporter_target_id"] for r in rows if r.get("reporter_target_id")})
    targets = _subjects(connection, project_id, [t for t in tids if t])
    names = {k: str(v.get("app_name") or k) for k, v in targets.items()}
    incidents = _incidents(connection, sorted({r["incident_id"] for r in rows
                                               if r.get("incident_id")}))
    grafana = _grafana(connection, project_id)
    counts = connection.execute(sql(
        "SELECT count(*) FILTER (WHERE state = 'firing') AS firing, "
        "       count(*) FILTER (WHERE state = 'pending') AS pending, "
        "       count(*) FILTER (WHERE state = 'firing' AND " + NEEDS_ATTENTION_SQL.replace(
            "a.", "") + ") AS attention "
        "FROM app_alerts WHERE project_id = :p" + (" AND subject_id = :t" if target_id else "")),
        {"p": project_id, **({"t": target_id} if target_id else {})}).mappings().first()
    apps = [{"target_id": t["id"], "app": t["app_name"], "env": t["environment"]}
            for t in _targets(connection, project_id).values()
            if connection.execute(sql("SELECT 1 FROM app_alert_rules WHERE subject_id = :t "
                                      "LIMIT 1"), {"t": t["id"]}).first()]
    apps += [{"target_id": r["id"], "app": r["name"], "env": r["environment"], "service": True}
             for r in connection.execute(sql(
                 "SELECT s.id, s.name, s.environment FROM monitored_services s WHERE "
                 "s.project_id = :p AND s.deployment_target_id IS NULL AND EXISTS (SELECT 1 FROM "
                 "app_alerts a WHERE a.service_id = s.id UNION SELECT 1 FROM app_alert_rules r "
                 "WHERE r.service_id = s.id)"), {"p": project_id}).mappings().all()]
    return {
        "project_id": project_id, "target_id": target_id, "state": state, "since": since,
        "severity": severity,
        "counts": {"firing": int(counts["firing"] or 0), "pending": int(counts["pending"] or 0),
                   "needs_attention": int(counts["attention"] or 0)},
        "attention": bool(attention), "severities": sev.catalog(),
        "alerts": [_alert_wire(connection, r, target=targets.get(r["subject_id"] or
                                                               r["target_id"], {}),
                               grafana=grafana, incidents=incidents, names=names) for r in rows],
        "apps": sorted(apps, key=lambda x: (str(x["app"]).lower(), str(x["env"]))),
        "grafana": {"state": grafana["state"], "reason": grafana["reason"] or None},
        # Open incidents, in the same words every card uses (incident_states): the page can
        # never say "0 firing" above a card that says "requests failing".
        "incidents": incident_states(connection, project_id, subject_id=target_id),
    }


_CMP = (">", ">=", "<", "<=", "==", "!=")


def _threshold(expr: str) -> tuple[Optional[str], Optional[float]]:
    """("> 5", min requests) read from the rule's PromQL: the comparison against a number at its
    top, and the `and … >= N` guard when it has one."""
    from . import promql_lite as pq

    try:
        node = pq.parse(expr)
    except pq.PromQLError:
        return None, None
    guard = None
    while isinstance(node, pq.Bin) and node.op in ("and", "unless"):
        rhs = node.rhs
        if (node.op == "and" and isinstance(rhs, pq.Bin) and rhs.op in (">=", ">")
                and isinstance(rhs.rhs, pq.Num)):
            guard = rhs.rhs.value
        node = node.lhs
    if isinstance(node, pq.Bin) and node.op in _CMP and isinstance(node.rhs, pq.Num):
        return f"{node.op} {aa.num(node.rhs.value)}", guard
    if isinstance(node, pq.Call) and node.func in ("absent", "absent_over_time"):
        return "absent", guard
    return None, guard


def _routing(connection, r: dict, target: dict) -> tuple[str, Optional[str]]:
    """Who an alert from this rule is told, from THE router (notify.preview): its words."""
    from . import monitoring as mon
    from . import notify

    labels = mon.alert_labels({"labels": r.get("labels") or {}, "alertname": r["alert"],
                               "rule_id": r["id"], "subject_id": target.get("id"),
                               "urgency": aa.urgency_of(r), "source_kind": "rule"}, target)
    point = connection.begin_nested()
    try:
        got = notify.preview(connection, target["project_id"], labels)
        point.commit()
    except Exception:  # noqa: BLE001 — routing words are for reading; never fail the tab
        point.rollback()
        return "Console", None
    names: list[str] = []
    for route in got.get("routes") or []:
        for c in route.get("contact_points") or []:
            if not c.get("skipped") and c.get("name") not in names:
                names.append(str(c.get("name")))
    urg = "high urgency" if got.get("urgency") == "high" else "low urgency"
    return (" · ".join(names) or "Console") + f" ({urg})", got.get("word") or None


def rule_facts(connection, r: dict, target: dict, interval_s: int) -> dict[str, Any]:
    """A rule as a query, plainly: its PromQL, how often it is checked, its window, `for`,
    `keep_firing_for`, threshold, min requests, routing and the datasource that evaluates it."""
    from . import promql_lite as pq

    rng = aa._max_range_ms(r["expr"])
    threshold, guard = _threshold(r["expr"])
    params = r.get("params") or {}
    if r["kind"] != "custom":
        p = {**aa.BUILTIN.get(r["kind"], {}).get("params", {}), **params}
        guard = p.get("min_requests", guard)
    routing, routing_tip = _routing(connection, r, target)
    return {"expr": r["expr"], "interval_s": int(interval_s),
            "window": pq.format_duration(rng) if rng else None,
            "for_s": int(r.get("for_seconds") or 0),
            "keep_firing_for_s": int(r.get("keep_firing_for_s") or 0),
            "threshold": threshold, "min_requests": guard,
            "routing": routing, "routing_tip": routing_tip,
            "datasource": {"prometheus": "Prometheus", "boss": "BOSS's own records"}.get(
                r.get("last_source") or "", "Not evaluated yet")}


def is_sample(r: dict) -> bool:
    return str((r.get("labels") or {}).get("sample") or "").lower() == "true"


def _interval(connection, subject_id: str) -> int:
    return int(connection.execute(sql(
        "SELECT interval_s FROM rule_groups WHERE subject_id = :s ORDER BY created_at LIMIT 1"),
        {"s": subject_id}).scalar() or 60)


def _rule_wire(connection, r: dict, target: dict, silences: dict[str, dict],
               interval_s: int = 60) -> dict[str, Any]:
    kind = r["kind"]
    b = aa.BUILTIN.get(kind, {})
    st = r.get("last_state") if r["enabled"] else "off"
    sil = silences.get(r["id"])
    tip = _STATE_TIPS.get(st)
    if st == "error":
        tip = r.get("last_word")
    link = r.get("respond_link") or {}
    return {
        "id": r["id"], "kind": kind, "label": b.get("label") or r["alert"], "alert": r["alert"],
        "expr": r["expr"], "for_seconds": int(r.get("for_seconds") or 0),
        "condition": aa.condition_words(kind, r["params"], int(r.get("for_seconds") or 0)),
        "params": r["params"] if kind != "custom" else {},
        "bounds": {k: list(v) for k, v in aa.PARAM_BOUNDS.get(kind, {}).items()},
        "severity": (r.get("labels") or {}).get("severity") or "warning",
        "sev": sev.level(r.get("labels") or {}), "sample": is_sample(r),
        "facts": rule_facts(connection, r, target, interval_s),
        "labels": r.get("labels") or {}, "summary": (r.get("annotations") or {}).get("summary"),
        "description": (r.get("annotations") or {}).get("description"),
        "runbook": _runbook((r.get("annotations") or {}).get("runbook_url") or "", target),
        "enabled": bool(r["enabled"]), "triage": bool(r.get("triage", True)),
        "auto_respond": bool(r.get("auto_respond")),
        "respond_on_behalf_of": _person(connection, link.get("on_behalf_of")),
        "notify": r.get("notify") or "console",
        "source": r["source"], "source_word": _SOURCE_WORDS.get(r["source"], r["source"]),
        # D4: a rule declared in the repo's boss.deploy.yaml (Set up monitoring writes them) is
        # repo-managed: versioned and reviewed in the app's pull requests.
        "managed_by": r.get("managed_by") or ("code" if r["source"] == "yaml" else "builtin"),
        "changed_by": _person(connection, r.get("changed_by")),
        "changed_at": _iso(r.get("changed_at")),
        "state": st, "state_word": "Silenced" if sil and st in ("firing", "pending") else
        _STATE_WORDS.get(st, st or ""), "state_tone": _STATE_TONES.get(st, "grey"),
        "state_tip": tip,
        "value": r.get("last_value"),
        "value_word": value_word(r.get("last_value"), b.get("unit", "")) if r.get(
            "last_value") is not None else None,
        "evaluated_at": _iso(r.get("last_eval_at")),
        "evaluated_by": _EVAL_WORDS.get(r.get("last_source") or "", None),
        "silence": ({"id": sil["id"], "ends_at": _iso(sil["ends_at"]), "reason": sil["reason"],
                     "by": _person(connection, sil["created_by"])} if sil else None),
        "editable": "params" if kind != "custom" else "promql",
    }


def _sources(connection, target: dict) -> list[dict[str, Any]]:
    """The apps (or outside systems) that report problems with this app through their signed
    channel: whoever reported about it, and the app's own channel when it declares one."""
    from . import incident_signals

    tid, pid = target["id"], target["project_id"]
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = connection.execute(sql(
        "SELECT a.reporter_target_id AS rid, max(a.active_at) AS last_at, "
        "       count(*) FILTER (WHERE a.active_at >= :since) AS today, "
        "       COALESCE(sum(a.value) FILTER (WHERE a.active_at >= :since), 0) AS reports "
        "FROM app_alerts a WHERE a.target_id = :t AND a.source_kind = 'signal' "
        "GROUP BY a.reporter_target_id"), {"t": tid, "since": since}).mappings().all()
    seen = {r["rid"]: dict(r) for r in rows if r["rid"]}
    declares = incident_signals.declarations(connection, [tid])
    if tid in declares and tid not in seen:
        seen[tid] = {"rid": tid, "last_at": None, "today": 0, "reports": 0}
    out = []
    names = _targets(connection, pid, sorted(seen)) if seen else {}
    for rid, r in seen.items():
        channel = connection.execute(sql(
            "SELECT last_polled_at, last_word FROM app_signal_channels WHERE target_id = :t"),
            {"t": rid}).mappings().first()
        token = incident_signals.token_status(connection, pid, rid)
        reporter = names.get(rid, {})
        out.append({
            "id": rid, "app": reporter.get("app_name") or rid, "kind": "signed reports",
            "connected": bool(token["minted"]),
            "last_report_at": _iso(r["last_at"]),
            "reports_today": int(float(r["reports"] or 0)),
            "last_poll_at": _iso((channel or {}).get("last_polled_at")),
            "last_word": (channel or {}).get("last_word") or None,
            "token": token,
        })
    return sorted(out, key=lambda s: str(s["app"]).lower())


def app_alerts_tab(connection, settings: Any, project_id: str, target_id: str, *,
                   state: str = "all", since: str = "24h") -> dict[str, Any]:
    """One app's Alerts tab: its rules, its signal sources and its history."""
    t = connection.execute(sql("SELECT * FROM deployment_targets WHERE id = :t "
                               "AND project_id = :p"), {"t": target_id, "p": project_id}
                           ).mappings().first()
    if not t:
        raise LookupError(f"No app {target_id} in this project.")
    target = dict(t)
    rule_rows = aa.rules(connection, target_id)
    if not rule_rows:
        # Rules exist from the first evaluation; a tab opened before it shows the starting
        # values instead of an empty list (written in a savepoint: a read never fails on it).
        point = connection.begin_nested()
        try:
            rule_rows = aa.ensure_rules(connection, target)
            point.commit()
        except Exception:  # noqa: BLE001
            point.rollback()
    now = datetime.now(timezone.utc)
    silences = aa.active_silences(connection, target_id, now)
    history = alerts(connection, project_id, target_id=target_id, state=state, since=since)
    changes = [{"rule_id": c["rule_id"], "change": c["change"], "reason": c["reason"] or None,
                "by": _person(connection, c["changed_by"]), "at": _iso(c["changed_at"])}
               for c in connection.execute(sql(
                   "SELECT rule_id, change, reason, changed_by, changed_at FROM "
                   "app_alert_rule_changes WHERE target_id = :t ORDER BY changed_at DESC "
                   "LIMIT 10"), {"t": target_id}).mappings().all()]
    from .observability_stack import endpoint_state

    prom = endpoint_state(connection, project_id, "prometheus")
    interval = _interval(connection, target_id)
    by_prom = any(r.get("last_source") == "prometheus" for r in rule_rows if r["enabled"])
    if by_prom:
        prom = {"state": "answering", "reason": ""}
    return {
        "target_id": target_id, "app": target["app_name"], "env": target["environment"],
        "rules": [_rule_wire(connection, r, target, silences, interval) for r in rule_rows],
        "severities": sev.catalog(), "has_samples": any(is_sample(r) for r in rule_rows),
        "evaluator": ("Prometheus" if prom["state"] == "answering"
                      else "BOSS's own monitoring"),
        "evaluator_tip": (None if prom["state"] == "answering" else
                          prom["reason"] or "Prometheus isn't answering"),
        "sources": _sources(connection, target),
        "history": history["alerts"], "counts": history["counts"],
        "state": state, "since": since, "changes": changes,
        "export": aa.export_rules(rule_rows),
        "grafana": history["grafana"],
    }


__all__ = ["alerts", "app_alerts_tab", "incident_states", "value_word"]
