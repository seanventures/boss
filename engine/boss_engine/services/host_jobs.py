"""THE HOST HELPER'S PULL QUEUE (`boss steward`; LLD §5.1 `host_jobs`, §7.2).

BOSS's API container cannot see the host's disk or reach Docker (no docker.sock is mounted, on
purpose: that would make the API root-equivalent on the machine). A small helper on the host
pulls TYPED jobs from here instead: no listener, no shell strings, one verb from an allow-list
with typed arguments, Ed25519-signed by the API, expiring, leased once. The helper verifies the
signature against `GET /v1/host-jobs/public-key`, checks `expires_at`, applies its OWN label gate
(images/volumes must carry `boss.install_id=<this install>`; backups must sit under BACKUP_ROOT and
match `boss-backup-*`), dry-runs, acts, and posts the result here.

Signing key: `BOSS_HOST_JOBS_SIGNING_KEY` (base64 raw 32-byte Ed25519 seed) when set; otherwise a
seed generated once and kept in `system_state['host_jobs_signing']`. The public key is published;
the seed never leaves the database / environment and is never logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .core import make_id

_log = logging.getLogger("boss.host_jobs")
SEED_SECRET_NAME = "host_jobs_signing_seed"
_SEED_CACHE: dict[str, bytes] = {}

VERBS = ("du", "prune_build_cache", "remove_images", "remove_orphan_volumes", "cap_logs",
         "docker_trim", "remove_backups")
DEFAULT_TTL = timedelta(minutes=30)
LEASE_TTL = timedelta(minutes=20)
STATE_KEY = "host_jobs_signing"


class HostJobError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def canonical(id_: str, host_id: str, verb: str, args: Mapping[str, Any], expires_at: Any) -> bytes:
    """The bytes both sides sign/verify: canonical JSON of the five fields, sorted keys."""
    exp = expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at)
    return json.dumps({"id": id_, "host_id": host_id, "verb": verb, "args": dict(args),
                       "expires_at": exp}, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


# ── keys ────────────────────────────────────────────────────────────────────────────────────

def _secret_backend():
    """The install's secret store as secrets_service picks it (secret_backends.default_secret_backend:
    managed store → vault → local encrypted vault → database). None when nothing answers."""
    try:
        from ..config import get_settings
        from . import secret_backends

        settings = get_settings()
        kind = secret_backends.default_secret_backend(settings)
        if not kind or str(kind).lower() in ("database", "db", "postgres"):
            return None          # the database IS system_state: nothing gained by going around
        return secret_backends.backend_for(str(kind), settings)
    except Exception:  # noqa: BLE001 — no store reachable: the fallback below says so
        _log.info("host-jobs signing seed: no secret store reachable; using system_state", exc_info=True)
        return None


def _seed(connection: Any) -> bytes:
    """The Ed25519 seed, in this order: BOSS_HOST_JOBS_SIGNING_KEY; the install's secret store
    (`host_jobs_signing_seed`); system_state only when no store exists. Generated once, cached."""
    raw = (os.environ.get("BOSS_HOST_JOBS_SIGNING_KEY") or "").strip()
    if raw:
        seed = base64.b64decode(raw)
        if len(seed) != 32:
            raise HostJobError("BOSS_HOST_JOBS_SIGNING_KEY must be a base64 32-byte Ed25519 seed")
        return seed
    if _SEED_CACHE.get("seed"):
        return _SEED_CACHE["seed"]
    backend = _secret_backend()
    if backend is not None:
        try:
            found = backend.get(SEED_SECRET_NAME)
            if found:
                _SEED_CACHE["seed"] = base64.b64decode(found)
                return _SEED_CACHE["seed"]
        except Exception:  # noqa: BLE001 — not there yet, or unreachable: try to write it
            pass
        seed = os.urandom(32)
        try:
            backend.put(SEED_SECRET_NAME, base64.b64encode(seed).decode("ascii"))
            _SEED_CACHE["seed"] = seed
            connection.execute(text(
                "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
                {"k": STATE_KEY, "v": json.dumps({"where": "secret_store", "name": SEED_SECRET_NAME,
                                                  "created_at": _now().isoformat()})})
            return seed
        except Exception:  # noqa: BLE001 — a store that cannot write: fall through
            _log.warning("host-jobs signing seed could not be written to the secret store", exc_info=True)
    value = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                               {"k": STATE_KEY}).scalar()
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and value.get("seed"):
        return base64.b64decode(str(value["seed"]))
    seed = os.urandom(32)
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": STATE_KEY, "v": json.dumps({"seed": base64.b64encode(seed).decode("ascii"),
                                          "where": "system_state", "created_at": _now().isoformat()})})
    return seed


def _private_key(connection: Any):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(_seed(connection))


def public_key(connection: Any) -> dict[str, str]:
    """`GET /v1/host-jobs/public-key`: raw 32 bytes, base64, with a key id (its SHA-256 prefix)."""
    from cryptography.hazmat.primitives import serialization

    raw = _private_key(connection).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"key_id": hashlib.sha256(raw).hexdigest()[:16], "algorithm": "Ed25519",
            "public_key": base64.b64encode(raw).decode("ascii")}


def sign(connection: Any, payload: bytes) -> str:
    return base64.b64encode(_private_key(connection).sign(payload)).decode("ascii")


def verify(public_key_b64: str, payload: bytes, signature_b64: str) -> bool:
    """The API-side check (tests; the helper has its own verifier)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(
            base64.b64decode(signature_b64), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


# ── the queue ───────────────────────────────────────────────────────────────────────────────

def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    args = row.get("args") or {}
    if isinstance(args, str):
        args = json.loads(args)
    result = row.get("result")
    if isinstance(result, str):
        result = json.loads(result)
    return {"id": row["id"], "host_id": row["host_id"], "action_id": row.get("action_id"),
            "verb": row["verb"], "args": args, "signature": row["signature"],
            "key_id": row.get("key_id") or "", "expires_at": _iso(row["expires_at"]),
            "status": row["status"], "leased_to": row.get("leased_to"),
            "lease_expires_at": _iso(row.get("lease_expires_at")), "result": result,
            "created_at": _iso(row.get("created_at")), "ended_at": _iso(row.get("ended_at"))}


def enqueue(connection: Any, *, host_id: str, verb: str, args: Mapping[str, Any],
            action_id: Optional[str] = None, ttl: timedelta = DEFAULT_TTL) -> dict[str, Any]:
    """A signed job for `host_id`. `args` always carries install_id, dry_run and (for a real
    run) plan_hash — the helper refuses a real run without one."""
    if verb not in VERBS:
        raise HostJobError(f"verb must be one of {', '.join(VERBS)}")
    job_id = make_id("hjob")
    expires_at = _now() + ttl
    payload = canonical(job_id, host_id, verb, args, expires_at)
    signature = sign(connection, payload)
    key = public_key(connection)["key_id"]
    row = connection.execute(text("""
        INSERT INTO host_jobs (id, host_id, action_id, verb, args, signature, key_id, expires_at,
                               status)
        VALUES (:id, :h, :a, :v, CAST(:args AS JSONB), :sig, :kid, :exp, 'queued') RETURNING *"""),
        {"id": job_id, "h": host_id, "a": action_id, "v": verb,
         "args": json.dumps(dict(args), default=str), "sig": signature, "kid": key,
         "exp": expires_at}).mappings().one()
    return _public(row)


def lease(connection: Any, *, host_id: str, leased_to: str) -> Optional[dict[str, Any]]:
    """Zero or one job for this host (oldest first; SKIP LOCKED so two helpers never share one).
    Expired jobs are marked so and never handed out."""
    connection.execute(text(
        "UPDATE host_jobs SET status = 'expired', ended_at = now() "
        "WHERE host_id = :h AND status = 'queued' AND expires_at <= now()"), {"h": host_id})
    row = connection.execute(text("""
        SELECT id FROM host_jobs WHERE host_id = :h AND expires_at > now() AND (
            status = 'queued' OR (status = 'leased' AND lease_expires_at < now()))
        ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"""), {"h": host_id}).first()
    if row is None:
        return None
    updated = connection.execute(text("""
        UPDATE host_jobs SET status = 'leased', leased_to = :to, lease_expires_at = :le
         WHERE id = :id RETURNING *"""),
        {"id": row[0], "to": leased_to[:120], "le": _now() + LEASE_TTL}).mappings().one()
    return _public(updated)


def record_result(connection: Any, job_id: str, result: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The helper's report: {ok, dry_run, items[], bytes, error?, plan_hash}."""
    ok = bool(result.get("ok"))
    clean = {"ok": ok, "dry_run": bool(result.get("dry_run")),
             "items": [str(i)[:300] for i in (result.get("items") or [])][:5000],
             "bytes": int(result.get("bytes") or 0), "plan_hash": str(result.get("plan_hash") or ""),
             "error": (str(result.get("error") or "")[:500] or None),
             "reported_at": _now().isoformat()}
    row = connection.execute(text("""
        UPDATE host_jobs SET status = :s, result = CAST(:r AS JSONB), ended_at = now()
         WHERE id = :id AND status IN ('leased', 'queued') RETURNING *"""),
        {"id": job_id, "s": "done" if ok else "failed", "r": json.dumps(clean)}).mappings().first()
    return _public(row) if row else None


def get(connection: Any, job_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM host_jobs WHERE id = :id"),
                             {"id": job_id}).mappings().first()
    return _public(row) if row else None


def for_action(connection: Any, action_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM host_jobs WHERE action_id = :a ORDER BY created_at"),
        {"a": action_id}).mappings().all()
    return [_public(r) for r in rows]


def last_seen(connection: Any, host_id: str) -> Optional[datetime]:
    """When the helper last reported anything (a sample or a result) for this host."""
    row = connection.execute(text("""
        SELECT greatest(
          (SELECT max(at) FROM storage_samples WHERE host_id = :h AND source = 'steward'),
          (SELECT max(ended_at) FROM host_jobs WHERE host_id = :h))"""), {"h": host_id}).scalar()
    return row


__all__ = ["DEFAULT_TTL", "HostJobError", "LEASE_TTL", "VERBS", "canonical", "enqueue",
           "for_action", "get", "last_seen", "lease", "public_key", "record_result", "sign",
           "verify"]
