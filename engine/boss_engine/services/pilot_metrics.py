"""BOSSAPP-1292: the numbers a guided preview is judged on, read from what BOSS already records.

A preview team is judged on three things, before and after BOSS:

  1. REVIEW TIME — how long a pull request waits from opened to approved. Opened is the build's
     ``code_task.pr_opened`` event; approved is its merge card (``decisions``) ``approved_at``.
     One PR opened by several attempts counts once: its first opening and its first approval.
  2. REGRESSIONS CAUGHT — a change whose tests failed was held from merge by the fail-closed gate
     (``quality_gates`` status ``fail`` with ``tests_passed`` false), and a change that failed after
     merge was stopped at deploy (``deploy.rolled_back``, ``change_set.rolled_back`` and
     ``change_set.reverted`` events).
  3. POST-DEPLOY ERRORS — alerts that fired on a running app (``app_alerts.fired_at``), how many
     notified a person, and how many BOSS remediated on its own: the alert's incident has a
     completed remediation (the governed fix pipeline) or a recovered recovery check.

The BEFORE numbers come from the customer (their pre-preview review time and so on) and are typed
in by a person: ``org_settings`` key ``pilot_baseline``. Nothing new is tracked about anyone; every
number here is computed on read, and every counted item names its source (a run, an incident) so a
number can be opened.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Optional

from sqlalchemy import text

from .spend_forecast import scope_label, scope_projects

BASELINE_KEY = "pilot_baseline"
#: The before-numbers a person may type, with their unit.
BASELINE_FIELDS = {
    "review_hours_median": "hours",
    "regressions_caught_per_month": "count",
    "post_deploy_alerts_per_month": "count",
    "auto_remediated_pct": "percent",
}
WINDOWS = (7, 30, 90)
ITEM_LIMIT = 200


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value or {})


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(v) for v in (value or [])] if isinstance(value, list) else []


def review_time(connection: Any, projects: list[str], since: datetime, until: datetime) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT e.project_id, e.entity_id AS run_id, e.created_at AS opened_at, e.data, "
        " d.approved_at, d.status AS decision_status "
        "FROM events e LEFT JOIN decisions d ON d.id = e.data->>'decision_id' "
        "WHERE e.event_type = 'code_task.pr_opened' AND e.project_id = ANY(:ids) "
        "AND e.created_at >= :since AND e.created_at < :until ORDER BY e.created_at"),
        {"ids": projects, "since": since, "until": until}).mappings().all()
    prs: dict[str, dict[str, Any]] = {}
    for r in rows:
        data = _data(r["data"])
        url = str(data.get("pull_request_url") or "") or f"run:{r['run_id']}"
        pr = prs.setdefault(url, {"pull_request_url": data.get("pull_request_url"),
                                  "run_id": r["run_id"], "project_id": r["project_id"],
                                  "opened_at": r["opened_at"], "approved_at": None})
        approved = r["approved_at"]
        if approved is not None and approved >= pr["opened_at"] and (
                pr["approved_at"] is None or approved < pr["approved_at"]):
            pr["approved_at"] = approved
    items, hours = [], []
    for pr in prs.values():
        h = None
        if pr["approved_at"] is not None:
            h = round((pr["approved_at"] - pr["opened_at"]).total_seconds() / 3600, 2)
            hours.append(h)
        items.append({**pr, "opened_at": _iso(pr["opened_at"]), "approved_at": _iso(pr["approved_at"]),
                      "hours": h})
    items.sort(key=lambda i: i["opened_at"] or "", reverse=True)
    return {"opened": len(prs), "approved": len(hours), "waiting": len(prs) - len(hours),
            "median_hours": round(median(hours), 2) if hours else None,
            "p90_hours": _pct(hours, 0.9),
            "mean_hours": round(sum(hours) / len(hours), 2) if hours else None,
            "items": items[:ITEM_LIMIT]}


def regressions(connection: Any, projects: list[str], since: datetime, until: datetime) -> dict[str, Any]:
    blocked = connection.execute(text(
        "SELECT q.code_task_run_id AS run_id, q.project_id, q.created_at, q.gates_failed, r.title "
        "FROM quality_gates q LEFT JOIN code_task_runs r ON r.id = q.code_task_run_id "
        "WHERE q.project_id = ANY(:ids) AND q.created_at >= :since AND q.created_at < :until "
        "AND q.status = 'fail' AND q.tests_passed = false ORDER BY q.created_at DESC"),
        {"ids": projects, "since": since, "until": until}).mappings().all()
    after = connection.execute(text(
        "SELECT e.event_type, e.entity_type, e.entity_id, e.project_id, e.created_at "
        "FROM events e WHERE e.project_id = ANY(:ids) AND e.created_at >= :since "
        "AND e.created_at < :until AND e.event_type IN "
        "('deploy.rolled_back', 'change_set.rolled_back', 'change_set.reverted') "
        "ORDER BY e.created_at DESC"),
        {"ids": projects, "since": since, "until": until}).mappings().all()
    before_items = [{"run_id": r["run_id"], "project_id": r["project_id"], "title": r["title"],
                     "at": _iso(r["created_at"]),
                     "gates_failed": _strings(r["gates_failed"])}
                    for r in blocked]
    after_items = [{"kind": r["event_type"], "run_id": r["entity_id"] if r["entity_type"] == "code_task_run" else None,
                    "change_set_id": r["entity_id"] if r["entity_type"] == "change_set" else None,
                    "project_id": r["project_id"], "at": _iso(r["created_at"])} for r in after]
    return {"blocked_before_merge": len(before_items), "stopped_after_merge": len(after_items),
            "total": len(before_items) + len(after_items),
            "before_merge_items": before_items[:ITEM_LIMIT], "after_merge_items": after_items[:ITEM_LIMIT]}


def post_deploy_errors(connection: Any, projects: list[str], since: datetime, until: datetime) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT a.id, a.project_id, a.alertname, a.fired_at, a.notified_at, a.incident_id, a.acked_by, "
        " EXISTS (SELECT 1 FROM remediations m WHERE m.incident_id = a.incident_id "
        "         AND m.status = 'completed') AS remediated, "
        " EXISTS (SELECT 1 FROM incident_recovery_checks k WHERE k.incident_id = a.incident_id "
        "         AND k.status = 'recovered') AS recovered, "
        " (SELECT m.code_task_run_id FROM remediations m WHERE m.incident_id = a.incident_id "
        "  ORDER BY m.created_at DESC LIMIT 1) AS fix_run_id "
        "FROM app_alerts a WHERE a.project_id = ANY(:ids) AND a.fired_at IS NOT NULL "
        "AND a.fired_at >= :since AND a.fired_at < :until ORDER BY a.fired_at DESC"),
        {"ids": projects, "since": since, "until": until}).mappings().all()
    items = []
    for r in rows:
        auto = bool(r["incident_id"]) and bool(r["remediated"] or r["recovered"])
        items.append({"alert_id": r["id"], "project_id": r["project_id"], "alertname": r["alertname"],
                      "fired_at": _iso(r["fired_at"]), "notified": r["notified_at"] is not None,
                      "incident_id": r["incident_id"], "auto_remediated": auto,
                      "fix_run_id": r["fix_run_id"], "handled_by_person": bool(r["acked_by"]) and not auto})
    alerted = len(items)
    auto = sum(1 for i in items if i["auto_remediated"])
    return {"alerted": alerted, "notified": sum(1 for i in items if i["notified"]),
            "auto_remediated": auto,
            "auto_remediated_pct": round(100 * auto / alerted, 1) if alerted else None,
            "items": items[:ITEM_LIMIT]}


def get_baseline(connection: Any, org_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT value, updated_by, updated_at FROM org_settings WHERE org_id = :o AND key = :k"),
        {"o": org_id, "k": BASELINE_KEY}).mappings().first()
    if not row:
        return {"values": {}, "note": "", "updated_by": None, "updated_at": None}
    value = _data(row["value"])
    return {"values": dict(value.get("values") or {}), "note": str(value.get("note") or ""),
            "updated_by": row["updated_by"], "updated_at": _iso(row["updated_at"])}


def set_baseline(connection: Any, org_id: str, values: dict[str, Any], note: str, actor: str) -> dict[str, Any]:
    """Store the customer's before-numbers. Unknown keys are refused; a blank one clears it."""
    clean: dict[str, float] = {}
    for key, raw in (values or {}).items():
        if key not in BASELINE_FIELDS:
            raise ValueError(f"baseline fields are {', '.join(BASELINE_FIELDS)}")
        if raw is None or raw == "":
            continue
        number = float(raw)
        if number < 0 or number != number:
            raise ValueError(f"{key} is a number of 0 or more")
        if BASELINE_FIELDS[key] == "percent" and number > 100:
            raise ValueError(f"{key} is a percent, 0 to 100")
        clean[key] = number
    connection.execute(text(
        "INSERT INTO org_settings (org_id, key, value, updated_by, updated_at) "
        "VALUES (:o, :k, CAST(:v AS JSONB), :by, now()) ON CONFLICT (org_id, key) DO UPDATE "
        "SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"o": org_id, "k": BASELINE_KEY, "by": actor,
         "v": json.dumps({"values": clean, "note": str(note or "")[:500]})})
    return get_baseline(connection, org_id)


def compute(connection: Any, org_id: str, *, scope_type: str = "org", scope_id: str = "",
            days: int = 30, now: Optional[datetime] = None) -> dict[str, Any]:
    from .core import now_utc

    if int(days) not in WINDOWS:
        raise ValueError(f"days is one of {', '.join(str(w) for w in WINDOWS)}")
    until = now or now_utc()
    since = until - timedelta(days=int(days))
    scope_type = str(scope_type or "org").strip().lower()
    scope_id = org_id if scope_type == "org" else str(scope_id or "").strip()
    projects = scope_projects(connection, org_id, scope_type, scope_id)
    baseline = get_baseline(connection, org_id)
    review = review_time(connection, projects, since, until)
    caught = regressions(connection, projects, since, until)
    errors = post_deploy_errors(connection, projects, since, until)
    per_month = 30.0 / int(days)
    return {
        "scope_type": scope_type, "scope_id": scope_id,
        "label": scope_label(connection, scope_type, scope_id),
        "days": int(days), "since": since.isoformat(), "until": until.isoformat(),
        "projects": projects,
        "review": review, "regressions": caught, "post_deploy": errors,
        "baseline": baseline,
        # The AFTER numbers in the baseline's units, so the two sit side by side.
        "after": {"review_hours_median": review["median_hours"],
                  "regressions_caught_per_month": round(caught["total"] * per_month, 1),
                  "post_deploy_alerts_per_month": round(errors["alerted"] * per_month, 1),
                  "auto_remediated_pct": errors["auto_remediated_pct"]},
    }


def to_csv(report: dict[str, Any]) -> str:
    """One CSV: a summary block (metric, before, after) then every counted item with its source."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "metric", "before", "after", "source", "project_id", "at", "detail"])
    before = report["baseline"]["values"]
    for key in BASELINE_FIELDS:
        w.writerow(["summary", key, before.get(key, ""), report["after"].get(key) if report["after"].get(key) is not None else "",
                    "", "", report["until"], f"last {report['days']} days, {report['label']}"])
    for i in report["review"]["items"]:
        w.writerow(["review", "pr_open_to_approval_hours", "", "" if i["hours"] is None else i["hours"],
                    i["run_id"], i["project_id"], i["opened_at"], i["pull_request_url"] or ""])
    for i in report["regressions"]["before_merge_items"]:
        w.writerow(["regressions", "blocked_before_merge", "", 1, i["run_id"], i["project_id"], i["at"],
                    ";".join(i["gates_failed"])])
    for i in report["regressions"]["after_merge_items"]:
        w.writerow(["regressions", "stopped_after_merge", "", 1, i["run_id"] or i["change_set_id"] or "",
                    i["project_id"], i["at"], i["kind"]])
    for i in report["post_deploy"]["items"]:
        w.writerow(["post_deploy", "auto_remediated" if i["auto_remediated"] else "alerted", "", 1,
                    i["incident_id"] or i["alert_id"], i["project_id"], i["fired_at"], i["alertname"]])
    return out.getvalue()
