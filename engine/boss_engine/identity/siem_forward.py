"""Stream new security-audit rows to one customer SIEM URL (BOSSAPP-1249).

The hash chain stays where ``audit.record`` left it. This module only reads
``identity_security_events`` and writes delivery state beside it. No saved
destination means no outbound call. A failed POST leaves the audit row and
records a retry, then a dead letter after three tries.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import audit

MAX_ATTEMPTS = 3
_TIMEOUT_S = 5.0


class DestinationRefused(ValueError):
    """The URL is not an http or https endpoint."""


def _sql(statement: str):
    from sqlalchemy import text
    return text(statement)


def _lock_key(org_id: str) -> int:
    raw = hashlib.sha256(f"identity_siem_forward:{org_id}".encode()).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


def _validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise DestinationRefused("Use an http or https URL.")
    raw = url.strip()
    if not raw or any(char in raw for char in ("\n", "\r", "\t", " ")):
        raise DestinationRefused("Use an http or https URL.")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise DestinationRefused("Use an http or https URL.")
    return raw


def save_destination(org_id: str, url: str, *, db: Any) -> dict[str, str]:
    """Store the org's one SIEM URL. A later save replaces it."""
    if not org_id or not isinstance(org_id, str):
        raise DestinationRefused("A destination needs an org.")
    clean = _validate_url(url)
    db.execute(_sql(
        "INSERT INTO identity_siem_destinations (org_id, url) VALUES (:o, :u) "
        "ON CONFLICT (org_id) DO UPDATE SET url = EXCLUDED.url, updated_at = now()"),
        {"o": org_id, "u": clean})
    return {"org_id": org_id, "url": clean}


def get_destination(org_id: str, *, db: Any) -> Optional[dict[str, str]]:
    row = db.execute(_sql(
        "SELECT org_id, url FROM identity_siem_destinations WHERE org_id = :o"),
        {"o": org_id}).mappings().first()
    if row is None:
        return None
    return {"org_id": row["org_id"], "url": row["url"]}


def clear_destination(org_id: str, *, db: Any) -> None:
    db.execute(_sql("DELETE FROM identity_siem_destinations WHERE org_id = :o"), {"o": org_id})


def failures(org_id: str, *, db: Any) -> list[dict[str, Any]]:
    """Retry and dead-letter rows. A sent row is not a failure."""
    rows = db.execute(_sql(
        "SELECT event_id, seq, status, attempts, last_error "
        "FROM identity_siem_deliveries "
        "WHERE org_id = :o AND status IN ('retry', 'dead') ORDER BY seq"),
        {"o": org_id}).mappings().all()
    return [{
        "event_id": int(row["event_id"]),
        "seq": int(row["seq"]),
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "error": row["last_error"] or "",
    } for row in rows]


def _pending(org_id: str, *, db: Any) -> list[Any]:
    columns = ", ".join(audit.HASHED_FIELDS)
    return db.execute(_sql(
        f"SELECT e.id, {columns}, e.prev_hash, e.hash, e.hash_alg "
        "FROM identity_security_events e "
        "WHERE e.org_id = :o AND NOT EXISTS ("
        "  SELECT 1 FROM identity_siem_deliveries d "
        "  WHERE d.org_id = e.org_id AND d.event_id = e.id "
        "    AND d.status IN ('sent', 'dead')) "
        "ORDER BY e.seq"), {"o": org_id}).mappings().all()


def _http_post(url: str, payload: bytes) -> tuple[int, str]:
    """POST one event. Returns (0, reason) when the socket itself fails."""
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return int(response.status), response.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(2048).decode("utf-8", "replace")
        return int(exc.code), body or str(exc.reason or "")
    except Exception as exc:  # noqa: BLE001 — the audit row must survive a dead endpoint
        return 0, f"{type(exc).__name__}: {exc}"


def _record_delivery(org_id: str, event_id: int, seq: int, *, db: Any, ok: bool,
                     http_status: int, detail: str) -> str:
    current = db.execute(_sql(
        "SELECT attempts FROM identity_siem_deliveries WHERE org_id = :o AND event_id = :e"),
        {"o": org_id, "e": event_id}).scalar()
    attempts = int(current or 0) + 1
    if ok:
        status, error = "sent", ""
    else:
        status = "dead" if attempts >= MAX_ATTEMPTS else "retry"
        snippet = " ".join((detail or "").split())[:400]
        if http_status:
            error = f"HTTP {http_status}" + (f": {snippet}" if snippet else "")
        else:
            error = snippet or "connection failed"
    db.execute(_sql(
        "INSERT INTO identity_siem_deliveries "
        "(org_id, event_id, seq, status, attempts, last_error) "
        "VALUES (:o, :e, :seq, :status, :attempts, :error) "
        "ON CONFLICT (org_id, event_id) DO UPDATE SET "
        "status = EXCLUDED.status, attempts = EXCLUDED.attempts, "
        "last_error = EXCLUDED.last_error, seq = EXCLUDED.seq, updated_at = now()"),
        {"o": org_id, "e": event_id, "seq": seq, "status": status, "attempts": attempts,
         "error": error})
    return status


def forward_new(org_id: str, *, db: Any) -> dict[str, int]:
    """POST each audit row that is not yet sent or dead-lettered.

    Returns immediately when the org has no destination, without opening a socket.
    A failed POST is recorded. The audit row is never updated or deleted.
    """
    if get_destination(org_id, db=db) is None:
        return {"posted": 0, "failed": 0}
    db.execute(_sql("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(org_id)})
    destination = get_destination(org_id, db=db)
    if destination is None:
        return {"posted": 0, "failed": 0}
    posted = 0
    failed = 0
    for raw in _pending(org_id, db=db):
        public = audit.to_public(audit.row_from_db(dict(raw)))
        payload = json.dumps(public, ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8")
        http_status, detail = _http_post(destination["url"], payload)
        ok = 200 <= http_status < 300
        _record_delivery(org_id, int(raw["id"]), int(raw["seq"]), db=db, ok=ok,
                         http_status=http_status, detail=detail)
        if ok:
            posted += 1
        else:
            failed += 1
    return {"posted": posted, "failed": failed}
