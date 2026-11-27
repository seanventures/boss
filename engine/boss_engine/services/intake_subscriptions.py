"""Provider-side intake subscription manager and renewer (P9c-1, BOSSAPP-1105; LLD §4.1, §13).

Keeps provider-side subscriptions alive for Outlook (Microsoft Graph change notifications),
Gmail (`users.watch`), and Salesforce (Pub/Sub logical subscriptions):
  * `ensure_subscription`: creates the provider subscription via `adapter.subscribe`, stores any
    `client_state` in the vault/secret store (NEVER plaintext in `intake_subscriptions`), stores
    only `client_state_hash`, and computes `renew_at`:
      - Outlook: `expires_at - max(25% of lifetime, 6h)`
      - Gmail: daily (`min(now + 24h, expires_at - 12h)`)
  * `renew_due`: leased singleton per subscription row (`FOR UPDATE SKIP LOCKED`) so two API
    instances never race on renewals. On renewal failure / `SubscriptionLost` / `removed`,
    recreates the subscription and runs immediate delta/history catch-up (`poll_due`) so no
    events are missed across gaps.
  * `handle_lifecycle`: handles Graph `reauthorizationRequired`, `subscriptionRemoved`, and
    `missed` lifecycle events (recreating and/or triggering delta catch-up with zero gaps).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.intake import REGISTRY
from ..connectors.intake.contract import (
    AuthFailed,
    RateLimited,
    Subscription,
    SubscriptionSpec,
)
from . import event_intake
from .core import emit_event, make_id

logger = logging.getLogger("boss.intake.subscriptions")

OUTLOOK_MIN_RENEW_MARGIN = timedelta(hours=6)
GMAIL_RENEW_CADENCE = timedelta(hours=24)
GMAIL_MIN_RENEW_MARGIN = timedelta(hours=12)
DEFAULT_RENEW_MARGIN = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _hash_state(client_state: str) -> str:
    return hashlib.sha256(client_state.encode("utf-8")).hexdigest()


def compute_renew_at(kind: str, expires_at: Optional[datetime], now: datetime) -> datetime:
    """When the renewer should renew this subscription before expiry (EVENTING_LLD.md §4.1)."""
    if expires_at is None:
        return now + GMAIL_RENEW_CADENCE
    lifetime = max(timedelta(minutes=5), expires_at - now)
    if kind == "gmail":
        # Google recommends daily renewal (7-day hard cap).
        target = min(now + GMAIL_RENEW_CADENCE, expires_at - GMAIL_MIN_RENEW_MARGIN)
        return max(now + timedelta(minutes=1), target)
    if kind == "outlook":
        # max(25% of lifetime, 6 hours) before expiry.
        margin = max(lifetime * 0.25, OUTLOOK_MIN_RENEW_MARGIN)
        target = expires_at - margin
        return max(now + timedelta(minutes=1), target)
    margin = max(lifetime * 0.25, DEFAULT_RENEW_MARGIN)
    return max(now + timedelta(minutes=1), expires_at - margin)


def ensure_subscription(connection: Connection, settings: Any, *, connection_id: str,
                        resource: str = "", notification_url: str = "",
                        lifetime_seconds: int = 0, actor: str = "boss",
                        now: Optional[datetime] = None) -> dict[str, Any]:
    """Create (or replace) a provider subscription for `connection_id` and persist it in
    `intake_subscriptions`. Any generated `client_state` is written to the secret store; only
    its SHA-256 hash lives in `intake_subscriptions`."""
    now = now or _now()
    crow = event_intake.get_connection(connection, connection_id, lock=True)
    if crow is None:
        raise event_intake.IntakeServiceError("no such intake connection.", status=404,
                                              code="not_found")
    kind = str(crow["kind"])
    adapter = REGISTRY.get(kind)
    if adapter is None or not adapter.can("subscribe"):
        raise event_intake.IntakeServiceError(f"{kind} does not support subscriptions.",
                                              status=422, code="subscribe_unsupported")
    conn = event_intake.to_intake_connection(connection, settings, crow)
    client_state = conn.secret("client_state") or ""
    if kind == "outlook" and not client_state:
        client_state = _secrets.token_urlsafe(32)
        ref = event_intake._store_secret(  # noqa: SLF001 — stores in vault/secret store only
            connection, str(crow["project_id"]), connection_id, "client_state", client_state, actor,
        )
        refs = dict(_json(crow.get("secret_refs"), {}) or {})
        refs["client_state"] = ref
        connection.execute(text(
            "UPDATE intake_connections SET secret_refs = CAST(:s AS JSONB), updated_at = now() "
            "WHERE id = :id"), {"s": json.dumps(refs), "id": connection_id})
        crow = event_intake.get_connection(connection, connection_id) or crow
        conn = event_intake.to_intake_connection(connection, settings, crow)

    spec = SubscriptionSpec(
        resource=resource or str(conn.get("resource") or conn.get("pubsub_topic")
                                 or conn.get("topic") or ""),
        client_state=client_state,
        notification_url=notification_url or str(conn.get("notification_url") or ""),
        lifetime_seconds=int(lifetime_seconds or 0),
    )
    created = adapter.subscribe(conn, spec)
    expires_at = _parse_dt(created.expires_at)
    renew_at = compute_renew_at(kind, expires_at, now)
    state_hash = _hash_state(client_state) if client_state else None
    sub_id = make_id("isub")
    meta = {"notification_url": spec.notification_url, "kind": kind}
    connection.execute(text("""
        INSERT INTO intake_subscriptions (
            id, connection_id, provider_subscription_id, resource, client_state_hash,
            expires_at, renew_at, status, metadata, last_renewed_at, created_at, updated_at)
        VALUES (:id, :c, :pid, :res, :hash, :exp, :ren, 'active', CAST(:m AS JSONB),
                :now, :now, :now)"""),
        {"id": sub_id, "c": connection_id, "pid": created.provider_subscription_id,
         "res": created.resource, "hash": state_hash, "exp": expires_at, "ren": renew_at,
         "m": json.dumps(meta), "now": now})
    emit_event(connection, str(crow["project_id"]), "intake.subscription_created", actor,
               "intake_subscription", sub_id,
               {"connection_id": connection_id, "kind": kind,
                "provider_subscription_id": created.provider_subscription_id,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "renew_at": renew_at.isoformat()})
    return {
        "id": sub_id,
        "connection_id": connection_id,
        "provider_subscription_id": created.provider_subscription_id,
        "resource": created.resource,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "renew_at": renew_at.isoformat(),
        "status": "active",
    }


def _recreate_and_catch_up(connection: Connection, settings: Any, crow: Mapping[str, Any],
                           sub_row: Mapping[str, Any], adapter: Any,
                           now: datetime) -> dict[str, Any]:
    """Recreate a lost/removed subscription and flag its cursor for immediate catch-up."""
    conn = event_intake.to_intake_connection(connection, settings, crow)
    meta = _json(sub_row.get("metadata"), {}) or {}
    client_state = conn.secret("client_state") or ""
    spec = SubscriptionSpec(
        resource=str(sub_row["resource"]),
        client_state=client_state,
        notification_url=str(meta.get("notification_url") or conn.get("notification_url") or ""),
    )
    created = adapter.subscribe(conn, spec)
    expires_at = _parse_dt(created.expires_at)
    renew_at = compute_renew_at(str(crow["kind"]), expires_at, now)
    connection.execute(text("""
        UPDATE intake_subscriptions
           SET provider_subscription_id = :pid, resource = :res, expires_at = :exp,
               renew_at = :ren, status = 'active', last_renewed_at = :now,
               last_error = NULL, updated_at = :now
         WHERE id = :id"""),
        {"pid": created.provider_subscription_id, "res": created.resource,
         "exp": expires_at, "ren": renew_at, "now": now, "id": sub_row["id"]})
    # Reset next_poll_at so the immediate catch-up poll runs without waiting for the interval.
    health = dict(_json(crow.get("health"), {}) or {})
    poll_state = dict(health.get("poll") or {})
    poll_state["next_poll_at"] = now.isoformat()
    health["poll"] = poll_state
    health["last_renew_at"] = now.isoformat()
    connection.execute(text(
        "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = :now "
        "WHERE id = :id"), {"h": json.dumps(health, default=str), "now": now, "id": crow["id"]})
    connection.execute(text("""
        INSERT INTO intake_cursors (connection_id, stream_key, resync_required, updated_at)
        VALUES (:c, :k, true, :now)
        ON CONFLICT (connection_id, stream_key) DO UPDATE
          SET resync_required = true, updated_at = :now"""),
        {"c": crow["id"], "k": event_intake.POLL_STREAM_KEY, "now": now})
    emit_event(connection, str(crow["project_id"]), "intake.subscription_recreated", "boss",
               "intake_subscription", str(sub_row["id"]),
               {"connection_id": str(crow["id"]), "kind": crow["kind"],
                "provider_subscription_id": created.provider_subscription_id,
                "expires_at": expires_at.isoformat() if expires_at else None})
    return {"recreated": True, "provider_subscription_id": created.provider_subscription_id,
            "expires_at": expires_at.isoformat() if expires_at else None}


def _run_catch_up_poll(engine: Any, settings: Any, connection_id: str, now: datetime) -> dict:
    """Trigger an immediate delta/history catch-up poll for `connection_id` regardless of mode."""
    from ..db import begin
    from ..connectors.intake.contract import Cursor, CursorExpired

    with begin(engine) as c:
        crow = event_intake.get_connection(c, connection_id)
        if crow is None or crow["status"] != "active":
            return {"polled": 0, "inserted": 0}
        adapter = REGISTRY.get(str(crow["kind"]))
        if adapter is None or not adapter.can("poll"):
            return {"polled": 0, "inserted": 0}
        c.execute(text("""
            INSERT INTO intake_cursors (connection_id, stream_key, cursor, cursor_time)
            VALUES (:c, :k, NULL, :now) ON CONFLICT DO NOTHING"""),
            {"c": connection_id, "k": event_intake.POLL_STREAM_KEY, "now": now})
        cur = c.execute(text("""
            SELECT * FROM intake_cursors WHERE connection_id = :c AND stream_key = :k
               FOR UPDATE SKIP LOCKED"""),
            {"c": connection_id, "k": event_intake.POLL_STREAM_KEY}).mappings().first()
        if cur is None:
            return {"polled": 0, "inserted": 0, "skipped": 1}
        conn = event_intake.to_intake_connection(c, settings, crow)
        cursor = Cursor(stream_key=event_intake.POLL_STREAM_KEY, value=cur.get("cursor"),
                        cursor_time=event_intake._iso(cur.get("cursor_time")))  # noqa: SLF001
        try:
            result = adapter.poll(conn, cursor, event_intake.PROCESSOR_BATCH)
        except CursorExpired:
            # Expired delta token (Graph 410) -> full fresh delta sync; inbox dedupe absorbs overlap.
            result = adapter.poll(
                conn, Cursor(stream_key=event_intake.POLL_STREAM_KEY, value=None,
                             cursor_time=now.isoformat()),
                event_intake.PROCESSOR_BATCH,
            )
        inserted = 0
        for raw in result.deliveries:
            if event_intake._insert_polled(c, crow, raw, now):  # noqa: SLF001
                inserted += 1
        nxt = result.next_cursor
        c.execute(text("""
            UPDATE intake_cursors
               SET cursor = :v, cursor_time = COALESCE(:t, cursor_time),
                   last_error = NULL, resync_required = false, updated_at = :now
             WHERE connection_id = :c AND stream_key = :k"""),
            {"v": nxt.value, "t": _parse_dt(nxt.cursor_time), "now": now,
             "c": connection_id, "k": event_intake.POLL_STREAM_KEY})
        return {"polled": 1, "inserted": inserted, "found": len(result.deliveries)}


def renew_due(engine: Any, settings: Any = None, *, now: Optional[datetime] = None,
              connection_id: Optional[str] = None) -> dict[str, Any]:
    """Renew every due subscription (leased via `FOR UPDATE SKIP LOCKED`). Recreates lost or
    removed subscriptions and catches up via delta/history poll so nothing is missed."""
    from ..db import begin
    from . import projects_archive

    settings = settings or event_intake._settings()  # noqa: SLF001
    now = now or _now()
    out = {"renewed": 0, "recreated": 0, "failed": 0, "catch_up_inserted": 0}
    catch_up_conns: list[str] = []

    with begin(engine) as c:
        ids = [str(r[0]) for r in c.execute(text(f"""
            SELECT s.id FROM intake_subscriptions s
              JOIN intake_connections ic ON ic.id = s.connection_id
             WHERE ic.status = 'active'
               AND {projects_archive.live_sql("ic.project_id")}
               AND (CAST(:cid AS TEXT) IS NULL OR s.connection_id = :cid)
               AND ((s.status IN ('active', 'renewing') AND (s.renew_at IS NULL OR s.renew_at <= :now))
                    OR s.status IN ('removed', 'reauthorization_required', 'failed'))
             ORDER BY s.renew_at NULLS FIRST"""),
            {"cid": connection_id, "now": now}).all()]

    for sub_id in ids:
        with begin(engine) as c:
            row = c.execute(text("""
                SELECT * FROM intake_subscriptions WHERE id = :id FOR UPDATE SKIP LOCKED"""),
                {"id": sub_id}).mappings().first()
            if row is None:
                continue
            crow = event_intake.get_connection(c, str(row["connection_id"]))
            if crow is None or crow["status"] != "active":
                continue
            adapter = REGISTRY.get(str(crow["kind"]))
            if adapter is None or not adapter.can("subscribe"):
                continue
            conn = event_intake.to_intake_connection(c, settings, crow)
            sub = Subscription(
                provider_subscription_id=str(row["provider_subscription_id"] or ""),
                resource=str(row["resource"]),
                expires_at=_parse_dt(row["expires_at"]).isoformat() if row["expires_at"] else None,
            )
            try:
                if row["status"] in ("removed", "failed") or not sub.provider_subscription_id:
                    _recreate_and_catch_up(c, settings, crow, row, adapter, now)
                    out["recreated"] += 1
                    catch_up_conns.append(str(crow["id"]))
                else:
                    try:
                        renewed = adapter.renew(conn, sub)
                        exp = _parse_dt(renewed.expires_at)
                        ren = compute_renew_at(str(crow["kind"]), exp, now)
                        c.execute(text("""
                            UPDATE intake_subscriptions
                               SET provider_subscription_id = :pid, expires_at = :exp,
                                   renew_at = :ren, status = 'active', last_renewed_at = :now,
                                   last_error = NULL, updated_at = :now
                             WHERE id = :id"""),
                            {"pid": renewed.provider_subscription_id, "exp": exp, "ren": ren,
                             "now": now, "id": sub_id})
                        c.execute(text("""
                            UPDATE intake_connections
                               SET health = health || CAST(:h AS JSONB), updated_at = :now
                             WHERE id = :cid"""),
                            {"h": json.dumps({"last_renew_at": now.isoformat()}),
                             "now": now, "cid": crow["id"]})
                        out["renewed"] += 1
                    except AuthFailed:
                        raise
                    except Exception:  # noqa: BLE001 — lost/404 subscription -> recreate + catch-up
                        _recreate_and_catch_up(c, settings, crow, row, adapter, now)
                        out["recreated"] += 1
                        catch_up_conns.append(str(crow["id"]))
            except AuthFailed as exc:
                err = f"auth_failed: {exc}"[:300]
                c.execute(text("""
                    UPDATE intake_subscriptions
                       SET status = 'reauthorization_required', last_error = :e, updated_at = :now
                     WHERE id = :id"""), {"e": err, "now": now, "id": sub_id})
                c.execute(text("""
                    UPDATE intake_connections
                       SET status = 'auth_failed',
                           health = health || CAST(:h AS JSONB), updated_at = :now
                     WHERE id = :cid"""),
                    {"h": json.dumps({"last_auth_error": err}), "now": now, "cid": crow["id"]})
                emit_event(c, str(crow["project_id"]), "intake.subscription_auth_failed", "boss",
                           "intake_subscription", sub_id,
                           {"connection_id": str(crow["id"]), "kind": crow["kind"], "error": err})
                out["failed"] += 1
            except RateLimited as exc:
                delay = int(exc.retry_after or 60)
                c.execute(text("""
                    UPDATE intake_subscriptions
                       SET renew_at = :nxt, last_error = :e, updated_at = :now
                     WHERE id = :id"""),
                    {"nxt": now + timedelta(seconds=delay), "e": f"rate_limited: {exc}"[:300],
                     "now": now, "id": sub_id})
                out["failed"] += 1
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"[:300]
                c.execute(text("""
                    UPDATE intake_subscriptions
                       SET status = 'failed', last_error = :e, updated_at = :now
                     WHERE id = :id"""), {"e": err, "now": now, "id": sub_id})
                out["failed"] += 1

    for cid in dict.fromkeys(catch_up_conns):
        try:
            polled = _run_catch_up_poll(engine, settings, cid, now)
            out["catch_up_inserted"] += int(polled.get("inserted") or 0)
        except Exception:  # noqa: BLE001 — poller will retry on next tick
            logger.warning("subscription catch-up poll failed for %s", cid, exc_info=True)
    return out


def handle_lifecycle(engine: Any, settings: Any, *, connection_id: str,
                     provider_subscription_id: str, lifecycle_event: str,
                     now: Optional[datetime] = None) -> dict[str, Any]:
    """Process a Microsoft Graph lifecycle notification (`subscriptionRemoved`, `missed`,
    `reauthorizationRequired`) by updating `intake_subscriptions`, recreating when needed, and
    running an immediate delta catch-up with zero gaps (P9c-T1)."""
    from ..db import begin

    now = now or _now()
    status_map = {
        "subscriptionRemoved": "removed",
        "reauthorizationRequired": "reauthorization_required",
        "missed": "active",
    }
    new_status = status_map.get(lifecycle_event, "active")
    with begin(engine) as c:
        c.execute(text("""
            UPDATE intake_subscriptions
               SET last_lifecycle = :lc, status = :st, updated_at = :now
             WHERE connection_id = :c
               AND (provider_subscription_id = :pid OR :pid = '')"""),
            {"lc": lifecycle_event, "st": new_status, "now": now,
             "c": connection_id, "pid": provider_subscription_id})
        if lifecycle_event == "missed":
            c.execute(text("""
                INSERT INTO intake_cursors (connection_id, stream_key, resync_required, updated_at)
                VALUES (:c, :k, true, :now)
                ON CONFLICT (connection_id, stream_key) DO UPDATE
                  SET resync_required = true, updated_at = :now"""),
                {"c": connection_id, "k": event_intake.POLL_STREAM_KEY, "now": now})
    result: dict[str, Any] = {"lifecycle_event": lifecycle_event, "connection_id": connection_id}
    if lifecycle_event in ("subscriptionRemoved", "reauthorizationRequired"):
        result["renewal"] = renew_due(engine, settings, now=now, connection_id=connection_id)
    elif lifecycle_event == "missed":
        result["catch_up"] = _run_catch_up_poll(engine, settings, connection_id, now)
    return result


__all__ = [
    "GMAIL_RENEW_CADENCE",
    "OUTLOOK_MIN_RENEW_MARGIN",
    "compute_renew_at",
    "ensure_subscription",
    "handle_lifecycle",
    "renew_due",
]
