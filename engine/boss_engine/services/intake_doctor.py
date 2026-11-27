"""Fleet / System Doctor playbooks for event intake & chat outbox (P9c-3, BOSSAPP-1107).

Four playbooks wired into `services/system_doctor.py`:
  1. `intake_subscription_lost` — lost, expired, removed, or overdue provider subscription
     (Outlook Graph / Gmail watch / Salesforce Pub/Sub). Safe auto-remedy:
     `repair_intake_subscription` (renews/recreates + triggers delta/history catch-up).
  2. `intake_poller_dead` — stalled poller or failing stream consumer (`failures >= 3` or
     overdue > 3x interval, non-auth), or stale `intake_processor` service heartbeat. Safe
     auto-remedy: `restart_intake_poller` (clears stale backoff/error and runs an immediate
     catch-up poll/stream pass).
  3. `intake_connection_auth_failed` — connection whose credentials were refused (401/403).
     Never auto-guesses credentials: opens a choice card with `intake_reconnect`,
     `pause_intake_connection`, `dismiss_today`.
  4. `chat_outbox_dead_letters` — dead-lettered replies in `chat_outbox`. Surfaces count and
     error class on a choice card with `retry_outbox_dead_letters` (skips permanent auth errors),
     `acknowledge`, `dismiss_today`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import event_intake

#: Auto-remedies safe for the doctor to execute on its own (non-destructive, rate-limited).
INTAKE_AUTO_ACTIONS = frozenset({
    "repair_intake_subscription",
    "restart_intake_poller",
})
#: Operator choices offered on intake / outbox cards.
INTAKE_OFFERED_OPTIONS = frozenset({
    "intake_reconnect",
    "pause_intake_connection",
    "retry_outbox_dead_letters",
})

SUBSCRIPTION_OVERDUE_MINUTES = 30
POLLER_STALE_MIN_SECONDS = 300
POLLER_FAILURE_THRESHOLD = 3
AUTH_ERROR_MARKERS = ("invalid_auth", "token_revoked", "account_inactive",
                      "messagewritesblocked", "401", "403", "auth_failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _opt(key: str, label: str, tip: str) -> dict[str, Any]:
    return {"key": key, "label": label, "tip": tip, "confirm": False}


def collect(connection: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Collect intake subscription, poller/stream, auth, and outbox dead-letter signals."""
    now = now or _now()
    lost_subs: list[dict[str, Any]] = []
    dead_pollers: list[dict[str, Any]] = []
    auth_failed: list[dict[str, Any]] = []
    outbox_dead: list[dict[str, Any]] = []

    # 1. Lost / expired / overdue subscriptions.
    sub_rows = connection.execute(text("""
        SELECT s.id, s.connection_id, s.provider_subscription_id, s.resource,
               s.expires_at, s.renew_at, s.status, s.last_lifecycle, s.last_error,
               ic.kind, ic.name, ic.project_id, ic.status AS conn_status
          FROM intake_subscriptions s
          JOIN intake_connections ic ON ic.id = s.connection_id
         WHERE ic.status = 'active'
           AND (s.status IN ('removed', 'failed', 'reauthorization_required')
                OR (s.expires_at IS NOT NULL AND s.expires_at < :now)
                OR (s.status = 'active' AND s.renew_at IS NOT NULL
                    AND s.renew_at < :overdue))
         ORDER BY s.renew_at NULLS FIRST LIMIT 20"""),
        {"now": now, "overdue": now - timedelta(minutes=SUBSCRIPTION_OVERDUE_MINUTES)}).mappings().all()
    for r in sub_rows:
        lost_subs.append({
            "subscription_id": str(r["id"]),
            "connection_id": str(r["connection_id"]),
            "provider_subscription_id": r["provider_subscription_id"],
            "resource": str(r["resource"]),
            "kind": str(r["kind"]),
            "name": str(r["name"] or r["kind"]),
            "project_id": str(r["project_id"] or ""),
            "status": str(r["status"]),
            "last_lifecycle": r["last_lifecycle"],
            "last_error": r["last_error"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        })

    # 2. Connections: auth_failed and dead pollers / streams.
    conn_rows = connection.execute(text("""
        SELECT id, project_id, kind, name, mode, status, config, health
          FROM intake_connections
         WHERE status IN ('active', 'auth_failed', 'rate_limited')
         ORDER BY created_at""")).mappings().all()
    for r in conn_rows:
        cid = str(r["id"])
        kind = str(r["kind"])
        name = str(r["name"] or kind)
        health = _json(r["health"], {}) or {}
        poll_st = dict(health.get("poll") or {})
        stream_st = dict(health.get("stream") or {})
        poll_err = str(poll_st.get("last_error") or "")
        stream_err = str(stream_st.get("last_error") or "")
        is_auth = (
            r["status"] == "auth_failed"
            or poll_err.lower().startswith("auth_failed")
            or stream_err.lower().startswith("auth_failed")
        )
        if is_auth:
            auth_failed.append({
                "connection_id": cid, "project_id": str(r["project_id"] or ""),
                "kind": kind, "name": name, "status": str(r["status"]),
                "error": (poll_err or stream_err or str(health.get("last_auth_error") or "")
                          or "provider refused credentials")[:300],
            })
            continue
        if r["status"] != "active":
            continue
        if r["mode"] == "poll":
            interval = event_intake.poll_seconds(_json(r["config"], {}) or {})
            stale_after = max(interval * 3, POLLER_STALE_MIN_SECONDS)
            last_poll = _parse_dt(poll_st.get("last_poll_at"))
            failures = int(poll_st.get("failures") or 0)
            overdue = last_poll is not None and (now - last_poll).total_seconds() > stale_after
            if failures >= POLLER_FAILURE_THRESHOLD or overdue:
                dead_pollers.append({
                    "connection_id": cid, "project_id": str(r["project_id"] or ""),
                    "kind": kind, "name": name, "mode": "poll",
                    "failures": failures, "overdue": overdue,
                    "last_poll_at": poll_st.get("last_poll_at"),
                    "last_error": poll_err[:300] or None,
                })
        elif r["mode"] in ("stream", "subscribe") and stream_st:
            failures = int(stream_st.get("failures") or 0)
            if failures >= POLLER_FAILURE_THRESHOLD or stream_st.get("status") == "error":
                dead_pollers.append({
                    "connection_id": cid, "project_id": str(r["project_id"] or ""),
                    "kind": kind, "name": name, "mode": str(r["mode"]),
                    "failures": failures, "overdue": False,
                    "last_event_at": stream_st.get("last_event_at"),
                    "last_error": stream_err[:300] or None,
                })

    # 3. Chat outbox dead letters grouped by endpoint.
    dead_rows = connection.execute(text("""
        SELECT o.endpoint_id, o.project_id, count(*) AS dead_count,
               max(o.last_error) AS sample_error, max(o.created_at) AS newest_at,
               e.name AS endpoint_name, e.kind AS endpoint_kind
          FROM chat_outbox o
          LEFT JOIN chat_endpoints e ON e.id = o.endpoint_id
         WHERE o.status = 'dead'
         GROUP BY o.endpoint_id, o.project_id, e.name, e.kind
         ORDER BY dead_count DESC LIMIT 10""")).mappings().all()
    for r in dead_rows:
        sample = str(r["sample_error"] or "")
        auth_like = any(tok in sample.lower() for tok in AUTH_ERROR_MARKERS)
        outbox_dead.append({
            "endpoint_id": str(r["endpoint_id"]),
            "project_id": str(r["project_id"] or ""),
            "endpoint_name": str(r["endpoint_name"] or r["endpoint_kind"] or "chat"),
            "endpoint_kind": str(r["endpoint_kind"] or "chat"),
            "dead_count": int(r["dead_count"] or 0),
            "sample_error": sample[:300],
            "auth_like": auth_like,
            "newest_at": r["newest_at"].isoformat() if r["newest_at"] else None,
        })

    return {
        "lost_subscriptions": lost_subs,
        "dead_pollers": dead_pollers,
        "auth_failed_connections": auth_failed,
        "outbox_dead_letters": outbox_dead,
    }


def diagnose(health: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pure diagnosis: turn collected intake/outbox signals into system_doctor findings."""
    findings: list[dict[str, Any]] = []
    if not health:
        return findings

    for sub in health.get("lost_subscriptions") or []:
        sid = sub["subscription_id"]
        name = sub.get("name") or sub.get("kind") or "source"
        status = sub.get("status") or "lost"
        findings.append({
            "kind": f"intake_subscription_lost:{sid}",
            "severity": "critical" if status in ("removed", "reauthorization_required") else "warn",
            "title": f"{name} event subscription needs repair ({status})",
            "detail": (f"Subscription for {sub.get('resource')} is {status}"
                       + (f" ({sub['last_error']})" if sub.get("last_error") else "")
                       + ". BOSS will renew/recreate it and catch up missed events."),
            "evidence": dict(sub),
            "options": [],
            "needs_choice": False,
            "target": sub["connection_id"],
            "auto": ["repair_intake_subscription"],
        })

    for poller in health.get("dead_pollers") or []:
        cid = poller["connection_id"]
        name = poller.get("name") or poller.get("kind") or "source"
        mode = poller.get("mode") or "poll"
        why = (f"{poller['failures']} consecutive failures" if poller.get("failures")
               else "poller overdue")
        findings.append({
            "kind": f"intake_poller_dead:{cid}",
            "severity": "warn",
            "title": f"{name} {mode} consumer stalled ({why})",
            "detail": (f"Last error: {poller['last_error']}. " if poller.get("last_error") else "")
                      + "BOSS will reset its backoff and run a catch-up pass.",
            "evidence": dict(poller),
            "options": [],
            "needs_choice": False,
            "target": cid,
            "auto": ["restart_intake_poller"],
        })

    for conn in health.get("auth_failed_connections") or []:
        cid = conn["connection_id"]
        name = conn.get("name") or conn.get("kind") or "source"
        findings.append({
            "kind": f"intake_connection_auth_failed:{cid}",
            "severity": "critical",
            "title": f"{name} intake connection failed authentication",
            "detail": (f"The provider refused credentials for {name} ({conn.get('error')}). "
                       "Reconnect or rotate credentials in Integrations › Events."),
            "evidence": dict(conn),
            "options": [
                _opt("intake_reconnect", "Reconnect source",
                     "Opens Integrations › Events to update credentials."),
                _opt("pause_intake_connection", "Pause connection",
                     "Pauses this connection until credentials are replaced."),
                _opt("dismiss_today", "Dismiss for today", "Hide this until tomorrow."),
            ],
            "needs_choice": True,
            "target": cid,
            "auto": [],
        })

    for dead in health.get("outbox_dead_letters") or []:
        eid = dead["endpoint_id"]
        count = int(dead.get("dead_count") or 0)
        name = dead.get("endpoint_name") or "chat"
        auth_note = (" Errors indicate revoked/blocked credentials — reconnect the chat app first."
                     if dead.get("auth_like") else "")
        findings.append({
            "kind": f"chat_outbox_dead_letters:{eid}",
            "severity": "critical" if dead.get("auth_like") else "warn",
            "title": f"{count} chat {'reply' if count == 1 else 'replies'} dead-lettered on {name}",
            "detail": (f"Last error: {dead.get('sample_error') or 'delivery failed'}." + auth_note),
            "evidence": dict(dead),
            "options": [
                _opt("retry_outbox_dead_letters", "Retry dead letters",
                     "Requeues transient dead-lettered replies (skips auth-blocked rows)."),
                _opt("acknowledge", "Acknowledge", "Close this notice without retrying."),
                _opt("dismiss_today", "Dismiss for today", "Hide this until tomorrow."),
            ],
            "needs_choice": True,
            "target": eid,
            "auto": [],
        })

    return findings


# ------------------------------------------------------------------------------------------ #
# Playbook remedies (auto + operator choices)
# ------------------------------------------------------------------------------------------ #

def repair_subscription(engine: Any, settings: Any, connection_id: str) -> str:
    """Auto-remedy: renew/recreate lost subscription and run catch-up."""
    from . import intake_subscriptions

    res = intake_subscriptions.renew_due(engine, settings, connection_id=connection_id)
    return json.dumps(res, sort_keys=True)


def restart_poller(engine: Any, settings: Any, connection_id: str) -> str:
    """Auto-remedy: clear backoff/error on a stalled poller or stream and run one catch-up pass."""
    from ..db import begin
    from . import intake_streams

    now = _now()
    mode = "poll"
    with begin(engine) as c:
        row = event_intake.get_connection(c, connection_id)
        if row is None:
            return "connection not found"
        mode = str(row["mode"])
        health = dict(_json(row.get("health"), {}) or {})
        if mode == "poll":
            poll_st = dict(health.get("poll") or {})
            poll_st["next_poll_at"] = now.isoformat()
            poll_st["failures"] = 0
            health["poll"] = poll_st
        else:
            stream_st = dict(health.get("stream") or {})
            stream_st["failures"] = 0
            stream_st["status"] = "restarting"
            health["stream"] = stream_st
        c.execute(text(
            "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = :now "
            "WHERE id = :id"), {"h": json.dumps(health, default=str), "now": now,
                                "id": connection_id})
        c.execute(text(
            "UPDATE intake_cursors SET last_error = NULL, updated_at = :now "
            "WHERE connection_id = :c"), {"now": now, "c": connection_id})
    if mode == "poll":
        res = event_intake.poll_due(engine, settings, connection_id=connection_id, now=now)
    else:
        res = intake_streams.consume_due(engine, settings, connection_id=connection_id, now=now)
    return json.dumps({"mode": mode, **res}, sort_keys=True)


def pause_connection_choice(engine: Any, settings: Any, row: dict, evidence: dict,
                            actor: str) -> dict[str, Any]:
    cid = str((evidence or {}).get("connection_id") or "")
    pid = str((evidence or {}).get("project_id") or "")
    if not cid or not pid:
        return {"result": "Connection no longer exists.", "status": "resolved"}
    with engine.begin() as c:
        event_intake.set_connection_status(
            c, project_id=pid, connection_id=cid, actor=actor, status="paused",
            reason="Paused via system doctor after auth failure",
        )
    return {"result": "Connection paused until credentials are replaced.",
            "status": "resolved", "target": cid}


def retry_outbox_dead_letters_choice(engine: Any, settings: Any, row: dict, evidence: dict,
                                     actor: str) -> dict[str, Any]:
    eid = str((evidence or {}).get("endpoint_id") or "")
    if not eid:
        return {"result": "No outbox endpoint named.", "status": "resolved"}
    with engine.begin() as c:
        rows = c.execute(text("""
            SELECT id, last_error FROM chat_outbox
             WHERE endpoint_id = :e AND status = 'dead'"""), {"e": eid}).mappings().all()
        retryable = [
            str(r["id"]) for r in rows
            if not any(tok in str(r["last_error"] or "").lower() for tok in AUTH_ERROR_MARKERS)
        ]
        skipped = len(rows) - len(retryable)
        if retryable:
            c.execute(text("""
                UPDATE chat_outbox
                   SET status = 'pending', attempts = 0, next_attempt_at = now(),
                       last_error = NULL
                 WHERE id = ANY(:ids)"""), {"ids": retryable})
    msg = f"Requeued {len(retryable)} dead-lettered chat {'reply' if len(retryable) == 1 else 'replies'}."
    if skipped:
        msg += f" Skipped {skipped} auth-blocked {'row' if skipped == 1 else 'rows'}."
    return {"result": msg, "status": "resolved" if not skipped else "chosen",
            "requeued": len(retryable), "skipped_auth": skipped, "target": eid}


__all__ = [
    "AUTH_ERROR_MARKERS",
    "INTAKE_AUTO_ACTIONS",
    "INTAKE_OFFERED_OPTIONS",
    "collect",
    "diagnose",
    "pause_connection_choice",
    "repair_subscription",
    "restart_poller",
    "retry_outbox_dead_letters_choice",
]
