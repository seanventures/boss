"""NOTHING STAYS ACKNOWLEDGED AND IDLE. An acknowledged incident gets an owner action in bounded time.

THE CASE (boss-dogfood, 2026-11-07). "Order API (staging): Monitoring rules dropped 11 rules ·
MonitoringRulesDropped", SEV3, firing since Oct 22, sat "Acknowledged · agent:sre" for hours with
nothing after the ack. An alert's ack lapses after four hours (monitoring.ACK_TTL) and it pages
again; an incident's ack never lapsed, so an agent's ack could park an incident for ever.

THE RULE. An incident acknowledged (by a person or an agent) with no owner action — no evidence,
note, verification or escalation on it — for ACK_IDLE_HOURS gets ONE of three, by the Shepherd,
and the incident records which and why (`finalize_ctx.ack_idle`, an evidence note, an event):

  * FIX IT — every alert still firing in it has a registered safe remedy (`REMEDIES`): the remedy
    runs, the alert resolves, the incident closes with the reason. MonitoringRulesDropped: the
    rules the manifest lost are still running in BOSS; BOSS adopts them as its own (console-
    managed: same rules, same thresholds, nothing changes in what is watched), so no monitoring
    depends on a manifest that no longer has them. Reversible: delete a rule in Alert rules.
  * CLOSE IT — nothing in it is firing any more: closed, saying so.
  * ESCALATE — anything else: ONE plain card naming the incident, how long it has been idle, what
    is still firing, and the recommended answer (take it, or close it). An incident already
    waiting on a person's card is not asked twice; the note says it waits there.

`sweep` rides the Shepherd's one-minute monitor tick (stuck_work.sweep). The invariant —
engine/tests/test_nothing_stays_acknowledged_and_idle.py — is that after a sweep no incident is
acknowledged with no owner action for longer than ACK_IDLE_HOURS.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger("boss.acked_idle")

#: How long an acknowledged incident may go without an owner action.
ACK_IDLE_HOURS = 4
ACTOR = "agent:shepherd"
#: The card a person gets when no safe fix exists (decision_actions.RECORDED_ON_APPROVAL).
CARD_ACTION = "incident_needs_owner"
#: Evidence kinds that are the incident's own record, not an owner acting on it.
_NOT_ACTION = ("alert", "deployment", "signal")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _span(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24} days"
    if hours >= 1:
        return f"{hours}h"
    return f"{max(1, int(delta.total_seconds() // 60))} min"


def due(connection: Any, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Incidents acknowledged with no owner action for ACK_IDLE_HOURS."""
    from . import projects_archive

    now = now or _now()
    horizon = now - timedelta(hours=ACK_IDLE_HOURS)
    rows = connection.execute(text(
        "SELECT i.*, GREATEST(i.acked_at, (SELECT max(e.created_at) FROM incident_evidence e "
        "  WHERE e.incident_id = i.id AND e.kind <> ALL(:not_action)), "
        "  CAST(i.finalize_ctx->'ack_idle'->>'at' AS timestamptz)) AS last_action "
        "FROM incidents i WHERE i.acked_by IS NOT NULL AND i.acked_at < :h "
        "AND COALESCE(i.lifecycle, '') NOT IN ('resolved', 'closed') "
        "AND COALESCE(i.status, '') NOT IN ('resolved', 'closed') AND "
        + projects_archive.live_sql("i.project_id") + " ORDER BY i.acked_at LIMIT 50"),
        {"h": horizon, "not_action": list(_NOT_ACTION)}).mappings().all()
    return [dict(r) for r in rows if r["last_action"] is None or r["last_action"] < horizon]


def _firing(connection: Any, incident_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "SELECT * FROM app_alerts WHERE incident_id = :i AND state IN ('pending', 'firing') "
        "ORDER BY fired_at"), {"i": incident_id}).mappings().all()]


# ── safe remedies, by alert name ────────────────────────────────────────────────────────────

def _adopt_dropped_rules(connection: Any, alert: Mapping[str, Any],
                         now: datetime) -> tuple[bool, str]:
    """MonitoringRulesDropped: the rules the manifest lost become BOSS's own (console-managed).
    They were running all along, so nothing changes in what is watched."""
    from .app_alerts import _audit, _resolve

    names = list(_json(alert.get("annotations")).get("dropped") or [])
    rules = [dict(r) for r in connection.execute(text(
        "SELECT * FROM app_alert_rules WHERE project_id = :p AND dropped_from_manifest_at IS NOT "
        "NULL AND (subject_id = :t OR target_id = :t) AND (CAST(:n AS text[]) IS NULL OR "
        "alert = ANY(CAST(:n AS text[])))"),
        {"p": alert["project_id"], "t": alert.get("target_id"),
         "n": names or None}).mappings().all()]
    if not rules and names:
        return False, "the dropped rules were not found, so BOSS could not adopt them"
    why = ("Adopted by the Shepherd: the app's boss.deploy.yaml dropped this rule and nobody acted "
           f"on the alert for {ACK_IDLE_HOURS}h; it keeps watching exactly as before, now "
           "managed in the console.")
    for rule in rules:
        before = {"source": rule.get("source"), "managed_by": rule.get("managed_by"),
                  "dropped_from_manifest_at": str(rule.get("dropped_from_manifest_at"))}
        connection.execute(text(
            "UPDATE app_alert_rules SET source = 'console', managed_by = 'console', "
            "dropped_from_manifest_at = NULL, changed_by = :by, changed_at = :now WHERE id = :id"),
            {"id": rule["id"], "by": ACTOR, "now": now})
        _audit(connection, rule, "adopted", before,
               {"source": "console", "managed_by": "console", "dropped_from_manifest_at": None},
               ACTOR, why, now)
    _resolve(connection, alert["id"], now, "rules_adopted")
    n = len(rules)
    return True, (f"adopted the {n} monitoring rule{'s' if n != 1 else ''} the manifest dropped "
                  "(they keep watching exactly as before, now managed in the console)")


#: alert name -> remedy(connection, alert, now) -> (fixed, what was done in words).
REMEDIES: dict[str, Callable[[Any, Mapping[str, Any], datetime], tuple[bool, str]]] = {
    "MonitoringRulesDropped": _adopt_dropped_rules,
}


# ── the three owner actions ─────────────────────────────────────────────────────────────────

def _close(connection: Any, inc: Mapping[str, Any], note: str, now: datetime) -> None:
    from .incident_response import _evidence

    connection.execute(text(
        "UPDATE incidents SET status = 'resolved', lifecycle = 'resolved', resolved_at = :now, "
        "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || jsonb_build_object('ack_idle', "
        "CAST(:a AS jsonb)) WHERE id = :id"),
        {"id": inc["id"], "now": now,
         "a": json.dumps({"at": now.isoformat(), "outcome": "closed", "why": note})})
    _evidence(connection, inc["id"], "note", ref="ack_idle", payload={"text": note}, at=now)


def _waiting_card(connection: Any, incident_id: str) -> Optional[str]:
    """An open card already asking a person about this incident (its squad's, or ours)."""
    from .decision_actions import OPEN_STATUSES

    return connection.execute(text(
        "SELECT d.id FROM decisions d WHERE d.status = ANY(:open) AND ("
        "  d.recommendation->>'incident_id' = :i OR d.id IN (SELECT w.needs_you_decision_id "
        "  FROM work_item_links l JOIN work_items w ON w.id = l.work_item_id "
        "  WHERE l.kind = 'incident' AND l.ref = :i AND w.needs_you_decision_id IS NOT NULL)) "
        "ORDER BY d.created_at LIMIT 1"),
        {"i": incident_id, "open": sorted(OPEN_STATUSES)}).scalar()


def _escalate(connection: Any, inc: Mapping[str, Any], firing: list[dict[str, Any]],
              idle: str, tried: str, now: datetime) -> Optional[str]:
    from .core import create_blocking_decision
    from .incident_response import _evidence

    title = (str(inc.get("title") or "An incident").splitlines() or [""])[0][:120]
    who = str(inc.get("acked_by") or "someone").replace("agent:", "the ").replace("_", " ")
    still = ", ".join(sorted({str(a.get("alertname")) for a in firing})[:3]) or "its alerts"
    diagnosis = (f"Acknowledged by {who} {idle} ago; nothing was done since. Still firing: "
                 f"{still}." + (f" {tried}" if tried else "") + " BOSS has no safe fix for it.")
    card = _waiting_card(connection, inc["id"])
    if card is None:
        card = create_blocking_decision(
            connection, inc["project_id"], None,
            f"{title}: acknowledged {idle} ago and nobody has acted — take it, or close it?",
            {"action": CARD_ACTION, "incident_id": inc["id"], "diagnosis": diagnosis,
             "recommended": "Take it (open the incident and work it), or close it with a reason "
                            "if it no longer matters.",
             "why_a_person": "BOSS has no safe fix for what is still firing."},
            {}, "incident_acknowledged_idle", default_action="hold")
    connection.execute(text(
        "UPDATE incidents SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
        "jsonb_build_object('ack_idle', CAST(:a AS jsonb)) WHERE id = :id"),
        {"id": inc["id"], "a": json.dumps({"at": now.isoformat(), "outcome": "escalated",
                                           "why": diagnosis, "card_id": card})})
    _evidence(connection, inc["id"], "note", ref="ack_idle",
              payload={"text": f"Escalated by the Shepherd: {diagnosis}", "card_id": card}, at=now)
    return card


def handle(connection: Any, inc: Mapping[str, Any], *, now: Optional[datetime] = None
           ) -> dict[str, Any]:
    """Give one acknowledged-and-idle incident its owner action. Returns what was done."""
    from .core import emit_event

    now = now or _now()
    idle = _span(now - (inc.get("last_action") or inc.get("acked_at") or now))
    firing = _firing(connection, inc["id"])
    fixed, tried = [], ""
    for alert in firing:
        remedy = REMEDIES.get(str(alert.get("alertname") or ""))
        if remedy is None:
            continue
        try:
            with connection.begin_nested():
                done, words = remedy(connection, alert, now)
        except Exception as exc:  # noqa: BLE001 — a failed remedy escalates, never raises
            log.warning("acked_idle remedy failed alert=%s", alert.get("id"), exc_info=True)
            done, words = False, f"the safe fix failed ({type(exc).__name__})"
        if done:
            fixed.append(words)
        else:
            tried = f"BOSS tried: {words}."
    left = _firing(connection, inc["id"])
    if not left:
        note = ("Closed by the Shepherd: acknowledged and idle for "
                f"{idle}; " + ("BOSS " + "; ".join(fixed) + ", so nothing in it is firing."
                               if fixed else "every alert in it has cleared."))
        _close(connection, inc, note, now)
        outcome = {"outcome": "fixed" if fixed else "closed", "why": note}
    else:
        card = _escalate(connection, inc, left, idle, tried, now)
        outcome = {"outcome": "escalated", "card_id": card}
    emit_event(connection, inc["project_id"], "incident.ack_idle_handled", ACTOR, "incident",
               inc["id"], {**outcome, "idle": idle})
    return {"incident_id": inc["id"], **outcome}


def sweep(connection: Any, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every acknowledged-and-idle incident gets its owner action. One failure never stops the rest."""
    out = []
    for inc in due(connection, now=now):
        try:
            with connection.begin_nested():
                out.append(handle(connection, inc, now=now))
        except Exception:  # noqa: BLE001
            log.exception("acked_idle failed incident=%s", inc.get("id"))
    return out


__all__ = ["ACK_IDLE_HOURS", "CARD_ACTION", "REMEDIES", "due", "handle", "sweep"]
