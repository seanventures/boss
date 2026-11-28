"""Leased stream consumer runner for `stream`-capable intake adapters (P9c-2, BOSSAPP-1106).

Proves the `stream` contract slot (open_stream / consume / commit) with Salesforce Pub/Sub API
first, ready for Kafka / SQS / Google Pub/Sub / Azure Service Bus (BOSSAPP-1108) with zero schema
or runner changes:
  * Singleton lease per `(connection_id, stream_key)` via `intake_cursors FOR UPDATE SKIP LOCKED`.
  * Inserts consumed events into `intake_event_dedupe` + `intake_events` and advances
    `intake_cursors.cursor` in the SAME Postgres transaction, then commits the cursor on the
    stream handle after DB commit.
  * Mid-stream kill/crash rolls back cleanly: the next consume resumes from the last committed
    cursor with 0 gaps and 0 duplicate runs.
  * On :class:`CursorExpired` (e.g. Salesforce replay ID older than 72 h): marks `resync_required`,
    invokes `adapter.poll` for SOQL catch-up (dedupe absorbs overlap), resets cursor for fresh
    subscription, and emits `intake.stream_resynced`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from ..connectors.intake import REGISTRY
from ..connectors.intake.contract import (
    AuthFailed,
    Cursor,
    CursorExpired,
    RateLimited,
    StoredRaw,
)
from . import event_intake
from .core import emit_event

logger = logging.getLogger("boss.intake.streams")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _stream_key_for(crow: Mapping[str, Any]) -> str:
    cfg = _json(crow.get("config"), {}) or {}
    return str(cfg.get("topic") or cfg.get("stream_key") or "stream")


def _record_to_stored_raw(topic: str, record: Any) -> StoredRaw:
    ev = record.event
    wrapped = {
        "_boss_kind": "salesforce_event" if ev.source.startswith("salesforce://") else "stream_event",
        "topic": topic,
        "replay_id": str((ev.data or {}).get("replay_id") or record.position.value or ""),
        "event_id": ev.id,
        "source_kind": str((ev.data or {}).get("delivered_by") or "pubsub"),
        "payload": {
            "ChangeEventHeader": {
                "entityName": (ev.data or {}).get("record_type"),
                "changeType": {
                    "created": "CREATE", "updated": "UPDATE", "deleted": "DELETE",
                }.get(str((ev.data or {}).get("operation") or ""), "UPDATE"),
                "recordIds": (ev.data or {}).get("record_ids") or [
                    (ev.data or {}).get("record_id")
                ],
                "changedFields": (ev.data or {}).get("changed_fields") or [],
                "commitUser": ev.bossactor.external_id,
                "commitTimestamp": ev.time,
                "commitNumber": (ev.data or {}).get("commit_number"),
                "transactionKey": (ev.data or {}).get("transaction_key") or "",
            },
            **dict((ev.data or {}).get("fields") or {}),
        } if ev.type.startswith("com.salesforce.cdc.") else dict((ev.data or {}).get("fields") or {}),
    }
    raw_bytes = json.dumps(wrapped, sort_keys=True, default=str).encode("utf-8")
    return StoredRaw(
        body=raw_bytes,
        headers={"content-type": "application/json"},
        dedupe_key=ev.bossdedupe or f"{ev.source}:{ev.id}",
    )


def consume_due(engine: Any, settings: Any = None, *, now: Optional[datetime] = None,
                connection_id: Optional[str] = None, max_events: int = 50,
                max_wait_ms: int = 250, holder: str = event_intake.HOLDER) -> dict[str, Any]:
    """Run one leased consume cycle across active stream-capable connections."""
    from ..db import begin
    from . import projects_archive

    settings = settings or event_intake._settings()  # noqa: SLF001
    now = now or _now()
    out = {"consumed": 0, "inserted": 0, "resynced": 0, "failed": 0, "skipped": 0}

    with begin(engine) as c:
        rows = [dict(r) for r in c.execute(text(f"""
            SELECT * FROM intake_connections
             WHERE status = 'active' AND mode IN ('stream', 'subscribe')
               AND {projects_archive.live_sql("project_id")}
               AND (CAST(:cid AS TEXT) IS NULL OR id = :cid)"""),
            {"cid": connection_id}).mappings()]

    for crow in rows:
        adapter = REGISTRY.get(str(crow["kind"]))
        if adapter is None or not adapter.can("stream"):
            continue
        stream_key = _stream_key_for(crow)
        with begin(engine) as c:
            c.execute(text("""
                INSERT INTO intake_cursors (connection_id, stream_key, cursor, cursor_time)
                VALUES (:c, :k, NULL, :now) ON CONFLICT DO NOTHING"""),
                {"c": crow["id"], "k": stream_key, "now": now})

        handle = None
        committed_cursor: Optional[Cursor] = None
        try:
            with begin(engine) as c:
                cur = c.execute(text("""
                    SELECT * FROM intake_cursors WHERE connection_id = :c AND stream_key = :k
                       FOR UPDATE SKIP LOCKED"""),
                    {"c": crow["id"], "k": stream_key}).mappings().first()
                if cur is None:
                    out["skipped"] += 1
                    continue
                crow = dict(c.execute(text("SELECT * FROM intake_connections WHERE id = :c"),
                                      {"c": crow["id"]}).mappings().first() or crow)
                conn = event_intake.to_intake_connection(c, settings, crow)
                cursor = Cursor(
                    stream_key=stream_key,
                    value=cur.get("cursor"),
                    cursor_time=event_intake._iso(cur.get("cursor_time")),  # noqa: SLF001
                )
                handle = adapter.open_stream(conn, cursor)
                try:
                    records = adapter.consume(handle, max_events, max_wait_ms)
                except CursorExpired as expired:
                    # Replay ID beyond 72 h retention -> SOQL catch-up + reset to LATEST.
                    c.execute(text("""
                        UPDATE intake_cursors SET resync_required = true, last_error = :e,
                               updated_at = :now
                         WHERE connection_id = :c AND stream_key = :k"""),
                        {"e": f"cursor_expired: {expired}"[:300], "now": now,
                         "c": crow["id"], "k": stream_key})
                    catchup_inserted = 0
                    if adapter.can("poll"):
                        polled = adapter.poll(conn, cursor, max_events)
                        for raw in polled.deliveries:
                            if event_intake._insert_polled(c, crow, raw, now,  # noqa: SLF001
                                                           verify_scheme="stream_catchup"):
                                catchup_inserted += 1
                    # Reset cursor to None so the next open_stream resubscribes from LATEST.
                    c.execute(text("""
                        UPDATE intake_cursors
                           SET cursor = NULL, cursor_time = :now, resync_required = false,
                               last_error = NULL, updated_at = :now
                         WHERE connection_id = :c AND stream_key = :k"""),
                        {"now": now, "c": crow["id"], "k": stream_key})
                    health = dict(_json(crow.get("health"), {}) or {})
                    health["stream"] = {
                        "stream_key": stream_key, "status": "resynced",
                        "last_resync_at": now.isoformat(),
                        "last_catchup_inserted": catchup_inserted,
                        "failures": 0, "last_error": None, "holder": holder[:80],
                    }
                    c.execute(text(
                        "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = :now "
                        "WHERE id = :id"),
                        {"h": json.dumps(health, default=str), "now": now, "id": crow["id"]})
                    emit_event(c, str(crow["project_id"]), "intake.stream_resynced", "boss",
                               "intake_connection", str(crow["id"]),
                               {"stream_key": stream_key, "catchup_inserted": catchup_inserted,
                                "reason": str(expired)[:200]})
                    out["resynced"] += 1
                    out["inserted"] += catchup_inserted
                    continue

                inserted = 0
                latest_pos = handle.position
                for rec in records:
                    stored = _record_to_stored_raw(stream_key, rec)
                    if event_intake._insert_polled(c, crow, stored, now,  # noqa: SLF001
                                                   verify_scheme="stream"):
                        inserted += 1
                    latest_pos = rec.position
                c.execute(text("""
                    UPDATE intake_cursors
                       SET cursor = COALESCE(:v, cursor),
                           cursor_time = COALESCE(:t, cursor_time),
                           last_error = NULL, resync_required = false, updated_at = :now
                     WHERE connection_id = :c AND stream_key = :k"""),
                    {"v": latest_pos.value,
                     "t": event_intake._event_time(latest_pos.cursor_time),  # noqa: SLF001
                     "now": now, "c": crow["id"], "k": stream_key})
                health = dict(_json(crow.get("health"), {}) or {})
                prev = dict(health.get("stream") or {})
                health["stream"] = {
                    "stream_key": stream_key, "status": "connected",
                    "cursor": latest_pos.value, "last_event_at": now.isoformat(),
                    "last_consumed": len(records), "last_inserted": inserted,
                    "total_inserted": int(prev.get("total_inserted") or 0) + inserted,
                    "failures": 0, "last_error": None, "holder": holder[:80],
                }
                c.execute(text(
                    "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = :now "
                    "WHERE id = :id"),
                    {"h": json.dumps(health, default=str), "now": now, "id": crow["id"]})
                committed_cursor = latest_pos
                out["consumed"] += len(records)
                out["inserted"] += inserted
            if committed_cursor is not None and handle is not None:
                adapter.commit(handle, committed_cursor)
        except AuthFailed as exc:
            err = f"auth_failed: {exc}"[:300]
            with begin(engine) as c:
                c.execute(text("""
                    UPDATE intake_connections
                       SET status = 'auth_failed',
                           health = health || CAST(:h AS JSONB), updated_at = :now
                     WHERE id = :id"""),
                    {"h": json.dumps({"stream": {"stream_key": stream_key, "status": "auth_failed",
                                                 "last_error": err, "last_attempt_at": now.isoformat()}}),
                     "now": now, "id": crow["id"]})
                c.execute(text("""
                    UPDATE intake_cursors SET last_error = :e, updated_at = :now
                     WHERE connection_id = :c AND stream_key = :k"""),
                    {"e": err, "now": now, "c": crow["id"], "k": stream_key})
            out["failed"] += 1
        except (RateLimited, Exception) as exc:  # noqa: BLE001 — tx rolled back, cursor preserved
            err = f"{type(exc).__name__}: {exc}"[:300]
            with begin(engine) as c:
                prev_health = _json(c.execute(text(
                    "SELECT health FROM intake_connections WHERE id = :id"),
                    {"id": crow["id"]}).scalar(), {}) or {}
                prev_stream = dict(prev_health.get("stream") or {})
                failures = int(prev_stream.get("failures") or 0) + 1
                prev_stream.update(stream_key=stream_key, status="error", failures=failures,
                                   last_error=err, last_attempt_at=now.isoformat(),
                                   holder=holder[:80])
                c.execute(text("""
                    UPDATE intake_connections
                       SET health = health || CAST(:h AS JSONB), updated_at = :now
                     WHERE id = :id"""),
                    {"h": json.dumps({"stream": prev_stream}), "now": now, "id": crow["id"]})
                c.execute(text("""
                    UPDATE intake_cursors SET last_error = :e, updated_at = :now
                     WHERE connection_id = :c AND stream_key = :k"""),
                    {"e": err, "now": now, "c": crow["id"], "k": stream_key})
            out["failed"] += 1
        finally:
            if handle is not None and isinstance(handle.native, dict):
                client = handle.native.get("client")
                if client is not None and hasattr(client, "close"):
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
    return out


__all__ = ["consume_due"]
