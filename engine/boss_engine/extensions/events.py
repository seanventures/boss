"""events.subscribe: a filtered feed of the durable events table, at least once, with an ack cursor.

FILTER (declared in the manifest, shown at install): `spec.events.kinds` — exact kinds
(`deploy.succeeded`), prefixes (`deploy.*`) or `*` — and only the projects the extension is
enabled in. An extension never sees its own events (actor `extension:<id>`).

ORDER AND CURSOR: events are read in (created_at, id) order, the order the realtime stream's
Last-Event-ID replay uses (idx_events_created_id). extension_event_cursors keeps, per (extension,
project), the last event the extension ACKED. A batch is delivered over the broker
(`events.deliver`); only when the call succeeds does the cursor move — to the event the extension
names in `{"ack": "<event id>"}`, or past the whole batch when it returns anything else. A failed
or timed-out delivery leaves the cursor where it was, so the same events come again: at least
once, never silently skipped. An extension must therefore treat an event id it has seen as done.

GRACE: an event becomes visible only `grace_seconds` after it was written. created_at is the
inserting statement's clock, and a transaction that started earlier can commit later; the grace
keeps a slow commit from landing behind a cursor that already moved past its timestamp.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .registry import InstalledExtension


def _kind_filter(kinds: list[str]) -> tuple[str, dict[str, Any]]:
    if "*" in kinds:
        return "TRUE", {}
    clauses, params = [], {}
    for i, k in enumerate(kinds):
        if k.endswith(".*"):
            clauses.append(f"event_type LIKE :k{i}")
            params[f"k{i}"] = k[:-2].replace("_", "\\_") + ".%"
        else:
            clauses.append(f"event_type = :k{i}")
            params[f"k{i}"] = k
    return "(" + " OR ".join(clauses) + ")", params


def cursor(connection: Any, extension_id: str, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT last_created_at, last_event_id, delivered FROM extension_event_cursors "
        "WHERE extension_id = :e AND project_id = :p"), {"e": extension_id, "p": project_id}
    ).mappings().first()
    return dict(row) if row else None


def pending(connection: Any, ext: InstalledExtension, project_id: str, *, limit: int,
            grace_seconds: float = 2.0) -> list[dict[str, Any]]:
    """The next events the extension has not acked, oldest first."""
    spec = ext.manifest.spec.events
    if spec is None:
        return []
    where, params = _kind_filter(spec.kinds)
    at = cursor(connection, ext.extension_id, project_id)
    after = ""
    if at and at.get("last_event_id"):
        after = " AND (created_at, id) > (:ca, :cid)"
        params.update(ca=at["last_created_at"], cid=at["last_event_id"])
    rows = connection.execute(text(
        "SELECT id, project_id, event_type, actor, entity_type, entity_id, data, created_at "
        f"FROM events WHERE project_id = :p AND {where}{after} AND actor <> :self "
        "AND created_at <= clock_timestamp() - make_interval(secs => :grace) "
        "ORDER BY created_at, id LIMIT :n"),
        {**params, "p": project_id, "self": f"extension:{ext.extension_id}",
         "grace": float(grace_seconds), "n": int(limit)}).mappings().all()
    out = []
    for r in rows:
        data = r["data"]
        out.append({"id": r["id"], "projectId": r["project_id"], "kind": r["event_type"],
                    "actor": r["actor"], "entityType": r["entity_type"], "entityId": r["entity_id"],
                    "data": json.loads(data) if isinstance(data, str) else data,
                    "createdAt": r["created_at"].isoformat(), "_at": r["created_at"]})
    return out


def ack(connection: Any, extension_id: str, project_id: str, event: dict[str, Any], count: int) -> None:
    connection.execute(text(
        "INSERT INTO extension_event_cursors (extension_id, project_id, last_created_at, "
        "last_event_id, delivered, updated_at) VALUES (:e, :p, :ca, :id, :n, now()) "
        "ON CONFLICT (extension_id, project_id) DO UPDATE SET last_created_at = EXCLUDED.last_created_at, "
        "last_event_id = EXCLUDED.last_event_id, "
        "delivered = extension_event_cursors.delivered + EXCLUDED.delivered, updated_at = now()"),
        {"e": extension_id, "p": project_id, "ca": event["_at"], "id": event["id"], "n": int(count)})


def pump(engine: Any, ext: InstalledExtension, project_id: str, *,
         grace_seconds: float = 2.0) -> dict[str, Any]:
    """Deliver one batch. {"ok", "delivered", "acked", "error"?}."""
    from . import broker

    if not ext.enabled_in(project_id):
        return {"ok": False, "delivered": 0, "acked": 0, "error": "not enabled in this project"}
    spec = ext.manifest.spec.events
    with engine.begin() as c:
        batch = pending(c, ext, project_id, limit=spec.batchSize if spec else 50,
                        grace_seconds=grace_seconds)
    if not batch:
        return {"ok": True, "delivered": 0, "acked": 0}
    wire = [{k: v for k, v in e.items() if k != "_at"} for e in batch]
    outcome = broker.call(engine, ext, "events.deliver", {"events": wire}, project_id=project_id)
    if not outcome.ok:
        return {"ok": False, "delivered": len(batch), "acked": 0, "error": outcome.error,
                "code": outcome.code}
    acked = len(batch)
    named = outcome.result.get("ack") if isinstance(outcome.result, dict) else None
    if named:
        ids = [e["id"] for e in batch]
        acked = ids.index(named) + 1 if named in ids else 0
    if acked:
        with engine.begin() as c:
            ack(c, ext.extension_id, project_id, batch[acked - 1], acked)
    return {"ok": True, "delivered": len(batch), "acked": acked}
