"""A span's full input and output: REDACTED, then stored with tiered retention (AP-P4, D8).

`agent_turns` keeps 4,000 characters of a turn and the ledger 8 KB of an event; a trace viewer
that shows "what went in and what came out" needs the whole thing. It is stored here, never as
it was:

  REDACTED BEFORE STORAGE. The JSON text is passed through the secret redactor first, so a value
  is judged WITH its key (`{"password": "hunter2"}`), then every string leaf through the full
  redactor — credentials, then PII (services/redaction.redact), and every secret a run was handed
  (run_secret_redaction, inside redact). What was replaced is counted per kind on the row
  (`redactions`), never kept.

  TIERS. `hot`: the body is a JSONB column (the viewer reads it in the same query). After
  HOT_DAYS a sweep moves it `cold`: to the payload backend — the install's S3 object store when
  S3_BUCKET is set (services/object_store.py, the platform's own store), else a local directory
  when BOSS_TRACE_PAYLOAD_DIR names one — keeping the key. With neither, it stays hot until it
  expires. A body bigger than MAX_HOT_BYTES goes straight to the backend; with no backend it is
  clipped (a marked preview), never stored whole in the database.

  RETENTION. A payload lives BASE_DAYS (14), or EXTENDED_DAYS (180) once its run met an
  evaluator, feedback or a dataset (`extend_retention`) — LangSmith's tiers (design §4.8).
  `expired` rows keep their metadata (size, digest, what was redacted) and no content. The span
  rows themselves are metadata, kept SPAN_DAYS (400, the ledger's D6 window).

The sweep runs from the resource reaper (resource_reaper.prune_tables) with everything else that
ages out; `PayloadBackend` is the interface a new store implements.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from sqlalchemy import text

log = logging.getLogger(__name__)

HOT_DAYS = 7
BASE_DAYS = 14
EXTENDED_DAYS = 180
SPAN_DAYS = 400
MAX_HOT_BYTES = 256 * 1024
PREVIEW_BYTES = 16 * 1024
ENV_DIR = "BOSS_TRACE_PAYLOAD_DIR"


# ── Redaction ──────────────────────────────────────────────────────────────────────────────────

def _leaves(value: Any, counts: dict, depth: int = 0) -> Any:
    from .redaction import redact

    if isinstance(value, str):
        cleaned, found = redact(value)
        for kind, n in found.items():
            counts[kind] = counts.get(kind, 0) + int(n)
        return cleaned
    if depth > 40:
        return "[too deep]"
    if isinstance(value, Mapping):
        return {str(k): _leaves(v, counts, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_leaves(v, counts, depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _leaves(str(value), counts, depth + 1)


def redact_payload(value: Any) -> tuple[Any, dict]:
    """(redacted value, {kind: count}). Key-aware first (the JSON text through the secret
    redactor), then every string leaf through the full one (credentials, PII, run secrets)."""
    from .redaction import redact_secrets

    counts: dict = {}
    try:
        body = json.dumps(value, default=str)
        whole_text = redact_secrets(body)
        whole = json.loads(whole_text)
        if whole_text != body:
            counts["SECRET"] = counts.get("SECRET", 0) + 1
    except (TypeError, ValueError):
        whole = value
    return _leaves(whole, counts), counts


# ── Backends ───────────────────────────────────────────────────────────────────────────────────

class PayloadBackend(Protocol):
    """Where a cold payload lives. Keys are relative '/'-separated paths."""

    name: str

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


class LocalFileBackend:
    """A directory: a single-box install, and the tests."""

    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        from .object_store import check_key

        return self.root / check_key(key)

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


class ObjectStoreBackend:
    """BOSS's own S3-compatible store (services/object_store.py), which speaks files."""

    name = "object_store"

    def __init__(self, store: Any, prefix: str = "trace-payloads"):
        self.store = store
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def put(self, key: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            name = handle.name
        try:
            self.store.put_file(self._key(key), Path(name))
        finally:
            os.unlink(name)

    def get(self, key: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload"
            self.store.get_file(self._key(key), path)
            return path.read_bytes()

    def delete(self, key: str) -> None:
        self.store.delete(self._key(key))


def backend_from_settings(settings: Any = None) -> Optional[PayloadBackend]:
    """The install's object store when S3_BUCKET is set, else BOSS_TRACE_PAYLOAD_DIR, else None
    (payloads stay hot until they expire)."""
    try:
        from .object_store import object_store_from_settings

        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        store = object_store_from_settings(settings)
        if store is not None:
            return ObjectStoreBackend(store)
    except Exception as exc:  # noqa: BLE001 — no store is a supported install
        log.debug("trace payloads: no object store (%s)", exc)
    folder = (os.environ.get(ENV_DIR) or "").strip()
    return LocalFileBackend(Path(folder)) if folder else None


# ── Writing ────────────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def object_key(project_id: str, run_id: str, payload_id: str) -> str:
    from .object_store import key_part

    return f"{key_part(project_id)}/{key_part(run_id)}/{key_part(payload_id)}.json"


def store(connection: Any, *, project_id: str, run_id: str, span_id: str, direction: str,
          value: Any, backend: Optional[PayloadBackend] = None,
          now: Optional[datetime] = None) -> str:
    """Redact `value` and store it; returns the payload id. A body over MAX_HOT_BYTES goes to
    the backend (or is clipped when there is none)."""
    from .core import make_id

    when = now or _now()
    cleaned, counts = redact_payload(value)
    body = json.dumps(cleaned, default=str)
    raw = body.encode("utf-8")
    payload_id = make_id("tpl")
    tier, stored_body, key, clipped = "hot", body, None, False
    if len(raw) > MAX_HOT_BYTES:
        if backend is not None:
            key = object_key(project_id, run_id, payload_id)
            backend.put(key, raw)
            tier, stored_body = "cold", None
        else:
            clipped = True
            stored_body = json.dumps({"_clipped": True, "bytes": len(raw),
                                      "preview": body[:PREVIEW_BYTES]})
    extended = bool(connection.execute(text(
        "SELECT 1 FROM trace_payloads WHERE run_id = :r AND extended LIMIT 1"),
        {"r": run_id}).first())
    days = EXTENDED_DAYS if extended else BASE_DAYS
    connection.execute(text("""
        INSERT INTO trace_payloads (id, project_id, run_id, span_id, direction, tier, body,
            object_key, bytes, sha256, redactions, clipped, extended, retain_until, created_at,
            moved_at)
        VALUES (:id, :p, :r, :s, :d, :tier, CAST(:body AS jsonb), :key, :bytes, :sha,
            CAST(:red AS jsonb), :clipped, :ext, :until, :at, :moved)
    """), {"id": payload_id, "p": project_id, "r": run_id, "s": span_id, "d": direction,
           "tier": tier, "body": stored_body, "key": key, "bytes": len(raw),
           "sha": hashlib.sha256(raw).hexdigest(), "red": json.dumps(counts),
           "clipped": clipped, "ext": extended, "until": when + timedelta(days=days),
           "at": when, "moved": when if tier == "cold" else None})
    return payload_id


def extend_retention(connection: Any, run_id: str) -> int:
    """The run met an evaluator, feedback, a dataset or an incident: keep its payloads
    EXTENDED_DAYS from when each was written. Returns rows extended."""
    done = connection.execute(text("""
        UPDATE trace_payloads SET extended = true,
               retain_until = GREATEST(retain_until, created_at + make_interval(days => :d))
         WHERE run_id = :r AND tier <> 'expired'
    """), {"r": run_id, "d": EXTENDED_DAYS})
    return int(done.rowcount or 0)


# ── Reading ────────────────────────────────────────────────────────────────────────────────────

def load(connection: Any, payload_id: str, *, backend: Optional[PayloadBackend] = None) -> dict:
    """{id, tier, body, bytes, redactions, clipped, retain_until}; a cold body is fetched from
    the backend, an expired one is None with the reason."""
    row = connection.execute(text(
        "SELECT id, tier, body, object_key, bytes, redactions, clipped, extended, retain_until "
        "FROM trace_payloads WHERE id = :i"), {"i": payload_id}).mappings().first()
    if row is None:
        return {"id": payload_id, "tier": "missing", "body": None}
    out = {"id": row["id"], "tier": row["tier"], "bytes": row["bytes"],
           "redactions": row["redactions"] or {}, "clipped": bool(row["clipped"]),
           "extended": bool(row["extended"]),
           "retain_until": row["retain_until"].isoformat() if row["retain_until"] else None,
           "body": row["body"]}
    if row["tier"] == "cold":
        store_ = backend if backend is not None else backend_from_settings()
        if store_ is None:
            out["body"], out["note"] = None, "stored in a payload backend this host cannot reach"
        else:
            try:
                out["body"] = json.loads(store_.get(str(row["object_key"])).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — said, never raised
                out["body"], out["note"] = None, f"payload not readable: {type(exc).__name__}"
    elif row["tier"] == "expired":
        out["note"] = "past its retention: the content is gone, its size and digest remain"
    return out


# ── The sweep ──────────────────────────────────────────────────────────────────────────────────

def sweep(engine: Any, *, backend: Optional[PayloadBackend] = None,
          now: Optional[datetime] = None, batch: int = 500) -> dict:
    """Hot → cold after HOT_DAYS (when a backend exists), anything past retain_until → expired,
    span rows past SPAN_DAYS deleted. Each in bounded batches, each its own transaction."""
    when = now or _now()
    out = {"moved": 0, "expired": 0, "spans_deleted": 0}
    if backend is not None:
        with engine.begin() as c:
            rows = c.execute(text("""
                SELECT id, project_id, run_id, body::text AS body FROM trace_payloads
                 WHERE tier = 'hot' AND created_at < :cut AND retain_until > :now
                 ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED
            """), {"cut": when - timedelta(days=HOT_DAYS), "now": when, "n": batch}).mappings().all()
            for row in rows:
                key = object_key(row["project_id"], row["run_id"], row["id"])
                backend.put(key, str(row["body"] or "null").encode("utf-8"))
                c.execute(text("UPDATE trace_payloads SET tier = 'cold', body = NULL, "
                               "object_key = :k, moved_at = :now WHERE id = :i"),
                          {"k": key, "now": when, "i": row["id"]})
                out["moved"] += 1
    with engine.begin() as c:
        rows = c.execute(text("""
            SELECT id, tier, object_key FROM trace_payloads
             WHERE tier <> 'expired' AND retain_until <= :now
             ORDER BY retain_until LIMIT :n FOR UPDATE SKIP LOCKED
        """), {"now": when, "n": batch}).mappings().all()
        for row in rows:
            if row["tier"] == "cold" and row["object_key"]:
                store_ = backend if backend is not None else backend_from_settings()
                if store_ is not None:
                    try:
                        store_.delete(str(row["object_key"]))
                    except Exception as exc:  # noqa: BLE001 — the row still expires
                        log.warning("trace payloads: %s not deleted: %s", row["id"], exc)
            c.execute(text("UPDATE trace_payloads SET tier = 'expired', body = NULL, "
                           "moved_at = :now WHERE id = :i"), {"now": when, "i": row["id"]})
            out["expired"] += 1
    with engine.begin() as c:
        done = c.execute(text("""
            DELETE FROM trace_spans WHERE ctid IN (
              SELECT ctid FROM trace_spans WHERE started_at < :cut LIMIT :n)
        """), {"cut": when - timedelta(days=SPAN_DAYS), "n": batch * 10})
        out["spans_deleted"] = int(done.rowcount or 0)
    return out


__all__ = ["BASE_DAYS", "EXTENDED_DAYS", "HOT_DAYS", "LocalFileBackend", "MAX_HOT_BYTES",
           "ObjectStoreBackend", "PayloadBackend", "SPAN_DAYS", "backend_from_settings",
           "extend_retention", "load", "object_key", "redact_payload", "store", "sweep"]
