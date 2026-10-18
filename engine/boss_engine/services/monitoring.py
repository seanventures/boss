"""Monitoring's shared core (BOSSAPP-598, Phase 1a): monitored services, signal sources and their
tokens, the Postgres floor of metrics, label matchers, silences / maintenance / deploy holds /
inhibition, flap scoring, ack, the alert transition log and the monitoring audit.

ONE STATE MACHINE, ONE INCIDENT MODEL; SOURCES ARE ADAPTERS
----------------------------------------------------------
Every alert — a rule BOSS evaluates, an external tool's alert mirrored by its own key, a synthetic
check, a missed heartbeat, an app's own report — is a row in `app_alerts`, judged by app_alerts'
one lifecycle and grouped into one `incidents` model. This module holds the pieces every source
shares. It never calls the network.

THE PROJECT COMES FROM THE TOKEN
--------------------------------
A source's token is HMAC(server key, source id + salt): 256 bits, shown once, only its hash
stored (plus a 2-character hint). BOSS can re-derive it to check a signature; a database read
alone cannot. The payload never names its project: whatever project or service a body claims
is ignored, and the stamped labels are BOSS's (spec §9.1, closing G9).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from ..ports import sql
from . import promql_lite as pq
from .core import emit_event, make_id

SOURCE_KINDS = ("otlp", "remote_write", "alertmanager", "grafana", "pagerduty_v2", "sentry",
                "datadog", "cloudwatch_sns", "generic", "heartbeat")
#: Kinds whose sender can sign (X-BOSS-Signature over "<timestamp>.<body>").
SIGNING_KINDS = ("generic", "pagerduty_v2", "heartbeat")
ROTATION_GRACE = timedelta(hours=1)
TIMESTAMP_TOLERANCE_S = 300
NONCE_WINDOW = timedelta(minutes=10)
RATE_PER_MINUTE = 600
MAX_BODY_BYTES = 1024 * 1024
MAX_OTLP_BYTES = 4 * 1024 * 1024
#: Active series a project may hold on the floor (spec §9.3).
SERIES_BUDGET = 50_000
FLOOR_RETENTION = timedelta(days=7)
ACK_TTL = timedelta(hours=4)
#: A source that stops talking about an alert without resolving it: resolved after this.
QUIET_RESOLVE = timedelta(hours=24)
FLAP_WINDOW = 21
FLAP_HIGH, FLAP_LOW = 0.5, 0.3
DEPLOY_HOLD = timedelta(minutes=15)


class IngestRefused(ValueError):
    """Something BOSS will not take, with the HTTP status and the sentence the sender gets."""

    def __init__(self, status: int, word: str, source_id: Optional[str] = None):
        super().__init__(word)
        self.status, self.word, self.source_id = status, word, source_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v: Any, default: Any = None) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (v or None)


# --------------------------------------------------------------------------- #
# The audit: every change to rules, sources, contact points, policies, silences
# --------------------------------------------------------------------------- #
def audit(connection, project_id: str, object_kind: str, object_id: str, change: str, *,
          before: Any = None, after: Any = None, actor: str, reason: str = "",
          at: Optional[datetime] = None) -> None:
    at = at or _now()
    connection.execute(sql(
        "INSERT INTO monitoring_changes (id, project_id, object_kind, object_id, change, before, "
        " after, reason, changed_by, changed_at) VALUES (:id, :p, :k, :o, :c, CAST(:b AS JSONB), "
        " CAST(:a AS JSONB), :why, :by, :at)"),
        {"id": make_id("mch"), "p": project_id, "k": object_kind, "o": object_id, "c": change,
         "b": json.dumps(before, default=str) if before is not None else None,
         "a": json.dumps(after, default=str) if after is not None else None,
         "why": (reason or "")[:500], "by": actor, "at": at})
    emit_event(connection, project_id, f"monitoring.{object_kind}.{change}", actor,
               object_kind, object_id, {"reason": (reason or "")[:500]})


# --------------------------------------------------------------------------- #
# Monitored services
# --------------------------------------------------------------------------- #
def ensure_target_services(connection, project_id: Optional[str] = None) -> int:
    """Every deployment target BOSS deployed has a monitored service (R1.1). Idempotent."""
    rows = connection.execute(sql(
        "INSERT INTO monitored_services (id, project_id, name, environment, "
        " deployment_target_id, created_by) "
        "SELECT 'svc_' || substr(md5(t.id), 1, 16), t.project_id, t.app_name, t.environment, "
        " t.id, 'boss' FROM deployment_targets t WHERE t.app_name IS NOT NULL "
        " AND t.environment IS NOT NULL"
        + (" AND t.project_id = :p" if project_id else "")
        + " AND NOT EXISTS (SELECT 1 FROM monitored_services s WHERE s.deployment_target_id = t.id)"
        " ON CONFLICT DO NOTHING RETURNING id"), ({"p": project_id} if project_id else {}))
    return len(rows.all())


def service_for_target(connection, target_id: str) -> Optional[str]:
    return connection.execute(sql(
        "SELECT id FROM monitored_services WHERE deployment_target_id = :t"),
        {"t": target_id}).scalar()


def service(connection, project_id: str, service_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql(
        "SELECT * FROM monitored_services WHERE id = :s AND project_id = :p"),
        {"s": service_id, "p": project_id}).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["labels"] = _json(out.get("labels"), {}) or {}
    out["onboarding"] = _json(out.get("onboarding"), {}) or {}
    return out


def find_or_create_service(connection, project_id: str, name: str, environment: str = "",
                           *, actor: str = "boss", path: Optional[str] = None) -> dict[str, Any]:
    name = (name or "").strip()[:120] or "unnamed service"
    env = (environment or "").strip()[:60] or "production"
    connection.execute(sql(
        "INSERT INTO monitored_services (id, project_id, name, environment, created_by) "
        "VALUES (:id, :p, :n, :e, :by) ON CONFLICT (project_id, name, environment) DO NOTHING"),
        {"id": make_id("svc"), "p": project_id, "n": name, "e": env, "by": actor})
    sid = connection.execute(sql(
        "SELECT id FROM monitored_services WHERE project_id = :p AND name = :n "
        "AND environment = :e"), {"p": project_id, "n": name, "e": env}).scalar()
    if path:
        add_onboarding_path(connection, sid, path)
    return service(connection, project_id, sid)


def add_onboarding_path(connection, service_id: str, path: str) -> None:
    connection.execute(sql(
        "UPDATE monitored_services SET onboarding = jsonb_set(onboarding, '{paths}', "
        " (SELECT to_jsonb(ARRAY(SELECT DISTINCT x FROM jsonb_array_elements_text("
        "   COALESCE(onboarding->'paths', '[]'::jsonb) || to_jsonb(CAST(:p AS TEXT))) x)))), "
        " updated_at = now() WHERE id = :s"), {"s": service_id, "p": path})


def note_data(connection, service_id: str, at: datetime, *, series: int = 0,
              path: Optional[str] = None, recognised: Optional[str] = None) -> None:
    """First and last data the service sent (the Connect sheet's live check reads this)."""
    patch: dict[str, Any] = {"last_data_at": at.isoformat()}
    if recognised:
        patch["recognised"] = recognised
    connection.execute(sql(
        "UPDATE monitored_services SET onboarding = onboarding || CAST(:patch AS JSONB) || "
        " jsonb_build_object('first_data_at', COALESCE(onboarding->>'first_data_at', :at), "
        "   'events', COALESCE((onboarding->>'events')::int, 0) + 1, "
        "   'series', GREATEST(COALESCE((onboarding->>'series')::int, 0), :n)), "
        " updated_at = now() WHERE id = :s"),
        {"s": service_id, "patch": json.dumps(patch), "at": at.isoformat(), "n": int(series)})
    if path:
        add_onboarding_path(connection, service_id, path)


def subject_of(svc: Mapping[str, Any]) -> dict[str, Any]:
    """A monitored service as the subject app_alerts evaluates (the shape a target has)."""
    return {"id": svc["id"], "project_id": svc["project_id"], "app_name": svc["name"],
            "environment": svc["environment"], "subject_kind": "service",
            "service_id": svc["id"], "dialect": svc.get("dialect") or "boss"}


# --------------------------------------------------------------------------- #
# Signal sources: per-source tokens, rotation, signatures, limits, counts
# --------------------------------------------------------------------------- #
def _server_key() -> bytes:
    from ..identity.authn_support import server_key

    return server_key()


def derive_token(source_id: str, salt: str) -> str:
    raw = hmac.new(_server_key(), f"signal-source:{source_id}:{salt}".encode(),
                   hashlib.sha256).digest()
    return "bsrc_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_source(connection, project_id: str, *, kind: str, name: str, actor: str,
                  service_id: Optional[str] = None, require_signature: bool = False,
                  heartbeat_every_s: Optional[int] = None,
                  mapping: Optional[dict] = None) -> dict[str, Any]:
    """A new source and its token. The token is in the return value ONCE and nowhere else."""
    if kind not in SOURCE_KINDS:
        raise IngestRefused(422, f"kind must be one of {', '.join(SOURCE_KINDS)}")
    if require_signature and kind not in SIGNING_KINDS:
        raise IngestRefused(422, f"A {kind} sender cannot sign; it uses its token over TLS")
    if kind == "heartbeat":
        heartbeat_every_s = int(heartbeat_every_s or 300)
        if not 30 <= heartbeat_every_s <= 7 * 86400:
            raise IngestRefused(422, "A heartbeat is expected every 30 seconds to 7 days")
    sid, salt = make_id("src"), secrets.token_urlsafe(16)
    token = derive_token(sid, salt)
    connection.execute(sql(
        "INSERT INTO signal_sources (id, project_id, service_id, name, kind, token_hash, "
        " token_hint, token_salt, require_signature, heartbeat_every_s, mapping, created_by) "
        "VALUES (:id, :p, :svc, :n, :k, :h, :hint, :salt, :sig, :hb, CAST(:m AS JSONB), :by)"),
        {"id": sid, "p": project_id, "svc": service_id, "n": (name or kind)[:120], "k": kind,
         "h": token_hash(token), "hint": token[-2:], "salt": salt, "sig": bool(require_signature),
         "hb": heartbeat_every_s, "m": json.dumps(mapping or {}), "by": actor})
    audit(connection, project_id, "signal_source", sid, "created",
          after={"kind": kind, "name": name, "service_id": service_id}, actor=actor)
    return {"source": source_view(connection, project_id, sid), "token": token}


def rotate_source(connection, project_id: str, source_id: str, *, actor: str,
                  now: Optional[datetime] = None) -> dict[str, Any]:
    """A new token; the old one keeps working for an hour (spec §9.1)."""
    now = now or _now()
    row = connection.execute(sql(
        "SELECT id, token_hash, token_salt FROM signal_sources WHERE id = :i AND project_id = :p"),
        {"i": source_id, "p": project_id}).mappings().first()
    if not row:
        raise LookupError("No such source in this project")
    salt = secrets.token_urlsafe(16)
    token = derive_token(source_id, salt)
    connection.execute(sql(
        "UPDATE signal_sources SET prev_token_hash = token_hash, prev_token_salt = token_salt, "
        " prev_valid_until = :until, token_hash = :h, token_hint = :hint, token_salt = :salt, "
        " rotated_at = :now WHERE id = :i"),
        {"i": source_id, "until": now + ROTATION_GRACE, "h": token_hash(token),
         "hint": token[-2:], "salt": salt, "now": now})
    audit(connection, project_id, "signal_source", source_id, "rotated", actor=actor,
          reason="The old token keeps working for one hour", at=now)
    return {"source": source_view(connection, project_id, source_id), "token": token}


def _token_from(headers: Mapping[str, str], query: Mapping[str, str],
                body_token: Optional[str]) -> Optional[str]:
    h = {k.lower(): v for k, v in headers.items()}
    auth = (h.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    if auth.lower().startswith("basic "):
        try:
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            return password or user or None
        except (ValueError, UnicodeDecodeError):
            return None
    for name in ("x-boss-token", "x-api-key"):
        if h.get(name):
            return h[name].strip()
    return (query.get("token") or "").strip() or body_token or None


def sign(token: str, timestamp: str, body: bytes) -> str:
    """The signature a sender (or BOSS, to an outbound webhook) puts in X-BOSS-Signature."""
    return "v1=" + hmac.new(token.encode(), timestamp.encode() + b"." + body,
                            hashlib.sha256).hexdigest()


def refuse(connection, source_id: Optional[str], status: int, word: str,
           now: Optional[datetime] = None) -> IngestRefused:
    """Count the refusal and say it in the source's row; returns the exception to raise."""
    if source_id:
        now = now or _now()
        _count(connection, source_id, accepted=False, now=now)
        connection.execute(sql(
            "UPDATE signal_sources SET last_refusal_word = :w, last_refusal_at = :now "
            "WHERE id = :i"), {"i": source_id, "w": word[:300], "now": now})
    return IngestRefused(status, word, source_id)


def _refusal(source_id: Optional[str], status: int, word: str) -> IngestRefused:
    return IngestRefused(status, word, source_id)


def record_refusal(engine, exc: IngestRefused, now: Optional[datetime] = None) -> None:
    """Count a refusal and say it on the source, in its OWN transaction (the request's
    transaction rolled back with the refusal)."""
    if not exc.source_id:
        return
    from ..db import begin

    with begin(engine) as c:
        refuse(c, exc.source_id, exc.status, exc.word, now)


def _count(connection, source_id: str, *, accepted: bool, now: datetime) -> None:
    hour = now.replace(minute=0, second=0, microsecond=0)
    connection.execute(sql(
        "INSERT INTO signal_source_counts (source_id, hour, accepted, refused) VALUES "
        " (:s, :h, :a, :r) ON CONFLICT (source_id, hour) DO UPDATE SET "
        " accepted = signal_source_counts.accepted + EXCLUDED.accepted, "
        " refused = signal_source_counts.refused + EXCLUDED.refused"),
        {"s": source_id, "h": hour, "a": 1 if accepted else 0, "r": 0 if accepted else 1})


def accepted(connection, source_id: str, now: Optional[datetime] = None) -> None:
    now = now or _now()
    _count(connection, source_id, accepted=True, now=now)
    connection.execute(sql("UPDATE signal_sources SET last_seen_at = :now WHERE id = :i"),
                       {"i": source_id, "now": now})


def authenticate(connection, *, headers: Mapping[str, str], raw: bytes, kinds: Iterable[str],
                 source_id: Optional[str] = None, query: Optional[Mapping[str, str]] = None,
                 body_token: Optional[str] = None, max_bytes: int = MAX_BODY_BYTES,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """The source this request comes from, or IngestRefused with a sentence.

    By its token (Bearer, Basic password, X-BOSS-Token, ?token= or a PagerDuty routing_key), the
    old one too during the rotation hour; or, for a signing source named in the path, by an
    X-BOSS-Signature over "<X-BOSS-Timestamp>.<body>" within 5 minutes, each signature once
    (10-minute nonce cache). Then the size and the per-source rate."""
    now = now or _now()
    kinds = tuple(kinds)
    query = query or {}
    h = {k.lower(): v for k, v in headers.items()}
    token = _token_from(headers, query, body_token)
    row = None
    if token:
        th = token_hash(token)
        row = connection.execute(sql(
            "SELECT * FROM signal_sources WHERE token_hash = :h OR (prev_token_hash = :h AND "
            " prev_valid_until > :now)"), {"h": th, "now": now}).mappings().first()
        if row is None:
            if source_id:
                known = connection.execute(sql("SELECT id FROM signal_sources WHERE id = :i"),
                                           {"i": source_id}).scalar()
                if known:
                    raise _refusal(known, 401, "Wrong or expired token for this "
                                 "source (a rotated token works for one hour)")
            raise IngestRefused(401, "Unknown token: create a source in Observability › Connect "
                                     "an app and use the token it shows")
    elif source_id and h.get("x-boss-signature"):
        row = connection.execute(sql("SELECT * FROM signal_sources WHERE id = :i"),
                                 {"i": source_id}).mappings().first()
        if row is None:
            raise IngestRefused(404, "No such source")
        ts, sig = h.get("x-boss-timestamp") or "", h.get("x-boss-signature") or ""
        try:
            skew = abs(now.timestamp() - int(ts))
        except ValueError:
            raise _refusal(row["id"], 401, "X-BOSS-Timestamp must be Unix seconds") from None
        if skew > TIMESTAMP_TOLERANCE_S:
            raise _refusal(row["id"], 401, "The signature's timestamp is more than "
                         "5 minutes from BOSS's clock (a replay, or a clock to fix)")
        good = [derive_token(row["id"], row["token_salt"])]
        if row["prev_token_salt"] and row["prev_valid_until"] and row["prev_valid_until"] > now:
            good.append(derive_token(row["id"], row["prev_token_salt"]))
        if not any(hmac.compare_digest(sign(t, ts, raw), sig.strip()) for t in good):
            raise _refusal(row["id"], 401, "Bad signature: sign "
                         "\"<timestamp>.<raw body>\" with the source's token (HMAC-SHA256)")
        first = connection.execute(sql(
            "INSERT INTO signal_receipts (source_id, receipt_key, seen_at) VALUES (:s, :k, :now) "
            "ON CONFLICT DO NOTHING RETURNING receipt_key"),
            {"s": row["id"], "k": "sig:" + sig.strip()[:120], "now": now}).first()
        if first is None:
            raise _refusal(row["id"], 409, "This exact signed body was already "
                         "received (a replay)")
    else:
        raise IngestRefused(401, "No token: send Authorization: Bearer <source token>")
    src = dict(row)
    if source_id and src["id"] != source_id:
        raise _refusal(src["id"], 403, "This token belongs to another source")
    if not src["enabled"]:
        raise _refusal(src["id"], 403, "This source is switched off")
    if src["kind"] not in kinds:
        raise _refusal(src["id"], 400, f"This is a {src['kind']} source; it cannot "
                     f"send to this endpoint")
    if src["require_signature"] and not h.get("x-boss-signature"):
        raise _refusal(src["id"], 401, "This source must sign its requests "
                     "(X-BOSS-Timestamp, X-BOSS-Signature)")
    if len(raw) > max_bytes:
        raise _refusal(src["id"], 413, f"The body is larger than "
                     f"{max_bytes // (1024 * 1024)} MB")
    minute = connection.execute(sql(
        "SELECT COALESCE(sum(accepted + refused), 0) FROM signal_source_counts "
        "WHERE source_id = :s AND hour >= :h"),
        {"s": src["id"], "h": now.replace(minute=0, second=0, microsecond=0)}).scalar()
    recent = connection.execute(sql(
        "SELECT count(*) FROM signal_receipts WHERE source_id = :s AND seen_at > :since "
        "AND receipt_key LIKE 'ev:%'"), {"s": src["id"], "since": now - timedelta(minutes=1)}
    ).scalar()
    if int(recent or 0) >= RATE_PER_MINUTE or int(minute or 0) >= RATE_PER_MINUTE * 60:
        raise _refusal(src["id"], 429, f"More than {RATE_PER_MINUTE} events a minute "
                     "from this source; slow down or batch")
    src["mapping"] = _json(src.get("mapping"), {}) or {}
    return src


def source_view(connection, project_id: str, source_id: str,
                now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    rows = sources(connection, project_id, now=now, only=source_id)
    return rows[0] if rows else None


def sources(connection, project_id: str, *, now: Optional[datetime] = None,
            only: Optional[str] = None) -> list[dict[str, Any]]:
    """What the console shows: connected, last seen, accepted/refused in 24 h, the last refusal
    in words. Never the token, never any part of it but the 2-character hint."""
    now = now or _now()
    rows = connection.execute(sql(
        "SELECT s.id, s.name, s.kind, s.service_id, m.name AS service, m.environment, "
        " s.token_hint, s.require_signature, s.heartbeat_every_s, s.last_seen_at, "
        " s.last_refusal_word, s.last_refusal_at, s.enabled, s.created_at, s.rotated_at, "
        " s.prev_valid_until, "
        " COALESCE((SELECT sum(c.accepted) FROM signal_source_counts c WHERE c.source_id = s.id "
        "   AND c.hour > :since), 0) AS accepted_24h, "
        " COALESCE((SELECT sum(c.refused) FROM signal_source_counts c WHERE c.source_id = s.id "
        "   AND c.hour > :since), 0) AS refused_24h "
        "FROM signal_sources s LEFT JOIN monitored_services m ON m.id = s.service_id "
        "WHERE s.project_id = :p" + (" AND s.id = :only" if only else "")
        + " ORDER BY s.created_at"),
        {"p": project_id, "since": now - timedelta(hours=24),
         **({"only": only} if only else {})}).mappings().all()
    out = []
    for r in rows:
        d = {k: iso(v) if isinstance(v, datetime) else v for k, v in dict(r).items()}
        d["accepted_24h"], d["refused_24h"] = int(r["accepted_24h"]), int(r["refused_24h"])
        d["connected"] = bool(r["last_seen_at"])
        d["old_token_valid_until"] = iso(r["prev_valid_until"]) if r["prev_valid_until"] and \
            r["prev_valid_until"] > now else None
        d.pop("prev_valid_until", None)
        out.append(d)
    return out


def receipt(connection, source_id: str, key: str, now: datetime,
            alert_id: Optional[str] = None) -> bool:
    """True the first time `key` is seen for this source (idempotency), False after."""
    got = connection.execute(sql(
        "INSERT INTO signal_receipts (source_id, receipt_key, alert_id, seen_at) VALUES "
        " (:s, :k, :a, :now) ON CONFLICT DO NOTHING RETURNING receipt_key"),
        {"s": source_id, "k": key[:300], "a": alert_id, "now": now}).first()
    return got is not None


def prune(connection, now: Optional[datetime] = None) -> None:
    now = now or _now()
    connection.execute(sql("DELETE FROM signal_receipts WHERE seen_at < :c AND "
                           "receipt_key LIKE 'sig:%'"), {"c": now - NONCE_WINDOW})
    connection.execute(sql("DELETE FROM signal_receipts WHERE seen_at < :c"),
                       {"c": now - timedelta(days=2)})
    connection.execute(sql("DELETE FROM signal_source_counts WHERE hour < :c"),
                       {"c": now - timedelta(days=8)})
    connection.execute(sql("DELETE FROM metric_points WHERE t < :c"),
                       {"c": now - FLOOR_RETENTION})


# --------------------------------------------------------------------------- #
# The Postgres floor (T0): stamped series, written and read as Prometheus would hold them
# --------------------------------------------------------------------------- #
def labels_hash(labels: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(labels.items())).encode()).hexdigest()[:32]


def stamp(labels: Mapping[str, Any], *, project_id: str, svc: Mapping[str, Any]) -> dict[str, str]:
    """BOSS's labels win: a client-sent `project` (or service id) is overwritten (spec §5.2)."""
    out = {str(k): str(v) for k, v in labels.items() if v is not None}
    out.update({"project": project_id, "service_id": svc["id"], "service": svc["name"],
                "app": svc["name"], "environment": svc["environment"]})
    return out


def write_points(connection, project_id: str, svc: Mapping[str, Any],
                 points: list[tuple[dict[str, str], datetime, float]], *,
                 now: Optional[datetime] = None, cumulative_delta: Iterable[str] = ()) -> dict:
    """Store stamped points; {series, points, new_series}. Refuses past the project's series
    budget, in words. `cumulative_delta` names series (by labels hash) sent as deltas: they are
    added to the last value so the floor holds counters, as Prometheus would."""
    now = now or _now()
    delta = set(cumulative_delta)
    hashes: dict[str, tuple[dict, str]] = {}
    for labels, _t, _v in points:
        lh = labels_hash(labels)
        hashes.setdefault(lh, (labels, labels.get("__name__", "")))
    have = {r[0]: (r[1], r[2]) for r in connection.execute(sql(
        "SELECT labels_hash, id, last_value FROM metric_series WHERE project_id = :p "
        "AND labels_hash = ANY(:h)"), {"p": project_id, "h": list(hashes)}).all()}
    new = [h for h in hashes if h not in have]
    if new:
        count = connection.execute(sql(
            "SELECT count(*) FROM metric_series WHERE project_id = :p AND last_at > :since"),
            {"p": project_id, "since": now - timedelta(hours=1)}).scalar() or 0
        if int(count) + len(new) > SERIES_BUDGET:
            raise IngestRefused(429, f"This project already holds {int(count):,} active series "
                                     f"(the budget is {SERIES_BUDGET:,}); aggregate by fewer "
                                     "labels")
        for h in new:
            labels, name = hashes[h]
            sid = make_id("ms")
            connection.execute(sql(
                "INSERT INTO metric_series (id, project_id, service_id, name, labels, labels_hash) "
                "VALUES (:i, :p, :s, :n, CAST(:l AS JSONB), :h) ON CONFLICT DO NOTHING"),
                {"i": sid, "p": project_id, "s": svc["id"], "n": name,
                 "l": json.dumps(labels), "h": h})
        have.update({r[0]: (r[1], r[2]) for r in connection.execute(sql(
            "SELECT labels_hash, id, last_value FROM metric_series WHERE project_id = :p "
            "AND labels_hash = ANY(:h)"), {"p": project_id, "h": new}).all()})
    running: dict[str, float] = {}
    rows = []
    for labels, t, v in sorted(points, key=lambda p: p[1]):
        lh = labels_hash(labels)
        series_id, last = have[lh]
        if lh in delta:
            base = running.get(lh, float(last or 0.0))
            v = base + float(v)
            running[lh] = v
        rows.append({"s": series_id, "t": t, "v": float(v)})
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        connection.execute(sql(
            "INSERT INTO metric_points (series_id, t, v) SELECT * FROM unnest("
            " CAST(:s AS TEXT[]), CAST(:t AS TIMESTAMPTZ[]), CAST(:v AS DOUBLE PRECISION[])) "
            "ON CONFLICT (series_id, t) DO UPDATE SET v = EXCLUDED.v"),
            {"s": [r["s"] for r in chunk], "t": [r["t"] for r in chunk],
             "v": [r["v"] for r in chunk]})
    last_by: dict[str, tuple[datetime, float]] = {}
    for r in rows:
        if r["s"] not in last_by or r["t"] >= last_by[r["s"]][0]:
            last_by[r["s"]] = (r["t"], r["v"])
    for s, (t, v) in last_by.items():
        connection.execute(sql(
            "UPDATE metric_series SET last_value = :v, last_at = GREATEST(COALESCE(last_at, :t), "
            " :t) WHERE id = :s"), {"s": s, "t": t, "v": v})
    return {"series": len(hashes), "points": len(rows), "new_series": len(new)}


def floor_store(connection, project_id: str, now: datetime, range_ms: int, *,
                names: Optional[Iterable[str]] = None,
                service_id: Optional[str] = None) -> pq.Store:
    """The project's floor series (only its own: tenancy by construction), as a Store."""
    start = now - timedelta(milliseconds=range_ms + pq.LOOKBACK_MS)
    names = sorted(set(names or []))
    rows = connection.execute(sql(
        "SELECT s.id, s.labels, p.t, p.v FROM metric_series s JOIN metric_points p "
        " ON p.series_id = s.id WHERE s.project_id = :p AND p.t > :a AND p.t <= :z"
        + (" AND s.name = ANY(:names)" if names else "")
        + (" AND s.service_id = :svc" if service_id else "")
        + " ORDER BY s.id, p.t"),
        {"p": project_id, "a": start, "z": now, **({"names": names} if names else {}),
         **({"svc": service_id} if service_id else {})}).all()
    store = pq.Store()
    cur, labels, pts = None, None, []
    for sid, lab, t, v in rows:
        if sid != cur:
            if cur is not None:
                store.add(labels, pts)
            cur, labels, pts = sid, _json(lab, {}) or {}, []
        pts.append((int(t.timestamp() * 1000), float(v)))
    if cur is not None:
        store.add(labels, pts)
    return store


# --------------------------------------------------------------------------- #
# Label matchers (Alertmanager's)
# --------------------------------------------------------------------------- #
MATCH_OPS = ("=", "!=", "=~", "!~")


def validate_matchers(raw: Any, *, allow_empty: bool = False) -> list[dict[str, str]]:
    if isinstance(raw, str):
        raw = parse_matchers(raw)
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise ValueError("Give at least one matcher, like service=\"checkout\"")
    out = []
    for m in raw[:20]:
        if not isinstance(m, Mapping):
            raise ValueError("A matcher is {name, op, value}")
        name, op, value = str(m.get("name") or "").strip(), str(m.get("op") or "="), \
            str(m.get("value") if m.get("value") is not None else "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", name):
            raise ValueError(f"'{name}' is not a label name")
        if op not in MATCH_OPS:
            raise ValueError(f"A matcher's op is one of {', '.join(MATCH_OPS)}")
        if op in ("=~", "!~"):
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{name}{op}{value!r}: {exc}") from None
        out.append({"name": name, "op": op, "value": value[:300]})
    return out


def parse_matchers(text: str) -> list[dict[str, str]]:
    """'service="checkout", severity=~"crit.*"' -> matchers."""
    out = []
    for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"',
                         text or ""):
        out.append({"name": m.group(1), "op": m.group(2), "value": m.group(3)})
    return out


def matches(labels: Mapping[str, str], matchers: Iterable[Mapping[str, str]]) -> bool:
    for m in matchers or []:
        have = str(labels.get(m["name"], ""))
        op, val = m.get("op", "="), str(m.get("value", ""))
        if op == "=" and have != val:
            return False
        if op == "!=" and have == val:
            return False
        if op in ("=~", "!~"):
            try:
                hit = re.fullmatch(val, have) is not None
            except re.error:
                return False
            if hit != (op == "=~"):
                return False
    return True


def alert_labels(alert: Mapping[str, Any], subject: Optional[Mapping[str, Any]] = None
                 ) -> dict[str, str]:
    """What matchers see: the alert's labels plus BOSS's (alertname, alert_rule_id, service,
    environment, severity, urgency, source, boss_subject)."""
    labels = {str(k): str(v) for k, v in (_json(alert.get("labels"), {}) or {}).items()}
    labels["alertname"] = str(alert.get("alertname") or "")
    if alert.get("rule_id"):
        labels["alert_rule_id"] = str(alert["rule_id"])
    labels["boss_subject"] = str(alert.get("subject_id") or alert.get("target_id")
                                 or alert.get("service_id") or "")
    if subject:
        labels.setdefault("service", str(subject.get("app_name") or ""))
        labels.setdefault("environment", str(subject.get("environment") or ""))
    labels["urgency"] = str(alert.get("urgency") or "high")
    labels["source"] = str(alert.get("source_kind") or "rule")
    labels.setdefault("severity", "warning")
    return labels


# --------------------------------------------------------------------------- #
# Silences, maintenance windows (RRULE subset), deploy holds, inhibition
# --------------------------------------------------------------------------- #
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def parse_rrule(rrule: str) -> dict[str, Any]:
    """FREQ=DAILY|WEEKLY[;BYDAY=MO,TU][;INTERVAL=n] — the subset maintenance needs. Empty: once."""
    out: dict[str, Any] = {}
    for part in (rrule or "").upper().replace("RRULE:", "").split(";"):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    if out and out.get("FREQ") not in ("DAILY", "WEEKLY"):
        raise ValueError("A maintenance window repeats DAILY or WEEKLY (FREQ=DAILY or FREQ=WEEKLY)")
    days = [d for d in (out.get("BYDAY") or "").split(",") if d]
    if any(d not in _DAYS for d in days):
        raise ValueError("BYDAY takes MO,TU,WE,TH,FR,SA,SU")
    try:
        interval = int(out.get("INTERVAL") or 1)
    except ValueError:
        raise ValueError("INTERVAL is a whole number") from None
    return {"freq": out.get("FREQ"), "days": [_DAYS[d] for d in days],
            "interval": max(1, interval)}


def window_active(w: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """When the window's current occurrence ends, if `now` is inside one; else None."""
    from zoneinfo import ZoneInfo

    if not w.get("enabled", True):
        return None
    try:
        tz = ZoneInfo(str(w.get("timezone") or "UTC"))
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    start = w["starts_at"]
    dur = timedelta(minutes=int(w["duration_min"]))
    rule = parse_rrule(w.get("rrule") or "")
    if not rule["freq"]:
        return start + dur if start <= now < start + dur else None
    local_start = start.astimezone(tz)
    local_now = now.astimezone(tz)
    # occurrences on the days around now (a window may cross midnight)
    for back in range(0, 8 + int(dur.total_seconds() // 86400)):
        day = (local_now - timedelta(days=back)).date()
        occ = datetime.combine(day, local_start.timetz().replace(tzinfo=None), tzinfo=tz)
        if occ < local_start:
            continue
        days_since = (occ.date() - local_start.date()).days
        if rule["freq"] == "DAILY" and days_since % rule["interval"]:
            continue
        if rule["freq"] == "WEEKLY":
            if (days_since // 7) % rule["interval"]:
                continue
            allowed = rule["days"] or [local_start.weekday()]
            if occ.weekday() not in allowed:
                continue
        if occ <= local_now < occ + dur:
            return (occ + dur).astimezone(timezone.utc)
    return None


def suppressors(connection, project_id: str, now: datetime) -> list[dict[str, Any]]:
    """Every active reason to hold notification in the project: matcher silences, maintenance
    windows in an occurrence, and deploy holds (15 minutes after each BOSS deploy, `hold`: the
    alert stays visible and goes to the agent first; it is never muted)."""
    out = []
    for r in connection.execute(sql(
            "SELECT id, matchers, ends_at, kind, reason FROM silences WHERE project_id = :p "
            "AND expired_at IS NULL AND starts_at <= :now AND ends_at > :now ORDER BY ends_at"),
            {"p": project_id, "now": now}).mappings().all():
        out.append({"kind": r["kind"] if r["kind"] != "manual" else "silence", "id": r["id"],
                    "matchers": _json(r["matchers"], []) or [], "until": r["ends_at"],
                    "mode": "hold" if r["kind"] == "deploy_hold" else "mute",
                    "reason": r["reason"]})
    for w in connection.execute(sql(
            "SELECT * FROM maintenance_windows WHERE project_id = :p AND enabled"),
            {"p": project_id}).mappings().all():
        try:
            until = window_active(dict(w), now)
        except ValueError:
            until = None
        if until:
            out.append({"kind": "maintenance", "id": w["id"], "until": until,
                        "matchers": _json(w["matchers"], []) or [], "mode": w["mode"],
                        "reason": w["name"]})
    for r in connection.execute(sql(
            "SELECT target_id, max(COALESCE(finished_at, created_at)) AS at FROM stack_deployments "
            "WHERE project_id = :p AND status = 'healthy' AND COALESCE(finished_at, created_at) > "
            " :since GROUP BY target_id"),
            {"p": project_id, "since": now - DEPLOY_HOLD}).mappings().all():
        if r["at"] and r["at"] <= now:
            out.append({"kind": "deploy_hold", "id": f"deploy:{r['target_id']}",
                        "until": r["at"] + DEPLOY_HOLD, "mode": "hold",
                        "matchers": [{"name": "boss_subject", "op": "=",
                                      "value": r["target_id"]}],
                        "reason": "A deploy finished in the last 15 minutes"})
    return out


def inhibit_rules(connection, project_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "source_matchers": _json(r["source_matchers"], []),
             "target_matchers": _json(r["target_matchers"], []), "equal": _json(r["equal"], [])}
            for r in connection.execute(sql(
                "SELECT * FROM inhibit_rules WHERE enabled AND (project_id IS NULL OR "
                "project_id = :p) ORDER BY origin, created_at"), {"p": project_id}).mappings()]


def inhibited_by(alert_lab: Mapping[str, str], firing: list[tuple[str, dict[str, str]]],
                 rules: list[dict[str, Any]], self_id: str) -> Optional[str]:
    """The id of a firing alert that holds this one back under a declarative inhibit rule."""
    for rule in rules:
        if not matches(alert_lab, rule["target_matchers"]):
            continue
        for other_id, other in firing:
            if other_id == self_id or not matches(other, rule["source_matchers"]):
                continue
            if all(other.get(k) == alert_lab.get(k) for k in rule["equal"]):
                return other_id
    return None


# --------------------------------------------------------------------------- #
# Flap detection (weighted share of state changes over the last 21 evaluations)
# --------------------------------------------------------------------------- #
def flap_update(history: str, present: bool) -> tuple[str, float]:
    h = ((history or "") + ("1" if present else "0"))[-FLAP_WINDOW:]
    if len(h) < 3:
        return h, 0.0
    changes = [1 if h[i] != h[i - 1] else 0 for i in range(1, len(h))]
    n = len(changes)
    # oldest change weighs 0.8, newest 1.2 (Nagios' flap detection weighting)
    weights = [0.8 + 0.4 * (i / (n - 1 if n > 1 else 1)) for i in range(n)]
    score = sum(w * c for w, c in zip(weights, changes)) / sum(weights)
    return h, round(max(0.0, min(1.0, score)), 4)


def flap_state(was_flapping: bool, score: float) -> bool:
    """Above 0.5 it is flapping; it stops only below 0.3 (hysteresis)."""
    return score > FLAP_HIGH or (was_flapping and score >= FLAP_LOW)


# --------------------------------------------------------------------------- #
# Transitions and ack
# --------------------------------------------------------------------------- #
def transition(connection, *, alert_id: str, project_id: str, at: datetime,
               from_state: Optional[str], to_state: str, value: Optional[float] = None,
               reason: str = "", actor: str = "evaluator") -> None:
    connection.execute(sql(
        "INSERT INTO alert_transitions (id, alert_id, project_id, at, from_state, to_state, "
        " value, reason, actor) VALUES (:id, :a, :p, :at, :f, :t, :v, :r, :by)"),
        {"id": make_id("atr"), "a": alert_id, "p": project_id, "at": at, "f": from_state,
         "t": to_state, "v": value, "r": (reason or "")[:300], "by": actor})


def ack(connection, project_id: str, *, alert_ids: Iterable[str] = (),
        incident_id: Optional[str] = None, actor: str, via: str = "console",
        now: Optional[datetime] = None) -> dict[str, Any]:
    """Ack stops repeats and escalation; it never silences (the alert keeps its state and is
    still evaluated). An alert's ack expires after 4 hours; an incident's never."""
    now = now or _now()
    ids = list(alert_ids)
    if incident_id:
        found = connection.execute(sql(
            "UPDATE incidents SET acked_by = :by, acked_at = :now, lifecycle = CASE WHEN "
            " lifecycle = 'detected' THEN 'acknowledged' ELSE lifecycle END WHERE id = :i "
            "AND project_id = :p RETURNING id"),
            {"i": incident_id, "p": project_id, "by": actor, "now": now}).first()
        if not found:
            raise LookupError("No such incident in this project")
        ids += list(connection.execute(sql(
            "SELECT id FROM app_alerts WHERE incident_id = :i AND state = 'firing'"),
            {"i": incident_id}).scalars().all())
    done = []
    for aid in dict.fromkeys(ids):
        row = connection.execute(sql(
            "UPDATE app_alerts SET acked_by = :by, acked_at = :now, ack_expires_at = :until, "
            " updated_at = :now WHERE id = :a AND project_id = :p AND state IN ('pending', "
            " 'firing') RETURNING id, state, incident_id"),
            {"a": aid, "p": project_id, "by": actor, "now": now,
             "until": now + ACK_TTL}).mappings().first()
        if row:
            done.append(row["id"])
            transition(connection, alert_id=row["id"], project_id=project_id, at=now,
                       from_state=row["state"], to_state=row["state"],
                       reason=f"acknowledged ({via})", actor="person")
            if row["incident_id"]:
                connection.execute(sql(
                    "UPDATE incidents SET acked_by = COALESCE(acked_by, :by), acked_at = "
                    " COALESCE(acked_at, :now), lifecycle = CASE WHEN lifecycle = 'detected' "
                    " THEN 'acknowledged' ELSE lifecycle END WHERE id = :i"),
                    {"i": row["incident_id"], "by": actor, "now": now})
    if done:
        emit_event(connection, project_id, "alert.acknowledged", actor, "app_alert",
                   done[0], {"alert_ids": done, "via": via, "incident_id": incident_id})
    return {"acked": done, "until": iso(now + ACK_TTL)}


def acked_now(alert: Mapping[str, Any], now: datetime) -> bool:
    return bool(alert.get("acked_at")) and (alert.get("ack_expires_at") is None
                                            or alert["ack_expires_at"] > now)


ACTION_TOKEN_TTL = timedelta(hours=24)


def mint_action_token(connection, project_id: str, action: str, *, alert_id: Optional[str] = None,
                      incident_id: Optional[str] = None, contact_point_id: Optional[str] = None,
                      now: Optional[datetime] = None) -> str:
    """A signed single-use link token (ack or resolve), valid 24 h. Only its hash is kept."""
    now = now or _now()
    raw = "bact_" + secrets.token_urlsafe(32)
    connection.execute(sql(
        "INSERT INTO alert_action_tokens (token_hash, project_id, alert_id, incident_id, action, "
        " contact_point_id, expires_at, created_at) VALUES (:h, :p, :a, :i, :act, :cp, :exp, :now)"),
        {"h": token_hash(raw), "p": project_id, "a": alert_id, "i": incident_id, "act": action,
         "cp": contact_point_id, "exp": now + ACTION_TOKEN_TTL, "now": now})
    return raw


def peek_action_token(connection, token: str, now: Optional[datetime] = None
                      ) -> Optional[dict[str, Any]]:
    now = now or _now()
    row = connection.execute(sql("SELECT * FROM alert_action_tokens WHERE token_hash = :h"),
                             {"h": token_hash(token or "")}).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["usable"] = out["used_at"] is None and out["expires_at"] > now
    out["word"] = ("Already used" if out["used_at"] else "Expired (links work for 24 hours)"
                   if out["expires_at"] <= now else "")
    return out


def use_action_token(connection, token: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Spend the link: single use, 24 h. Returns what it did, or raises ValueError in words."""
    now = now or _now()
    row = connection.execute(sql(
        "UPDATE alert_action_tokens SET used_at = :now, used_by = 'email link' "
        "WHERE token_hash = :h AND used_at IS NULL AND expires_at > :now RETURNING *"),
        {"h": token_hash(token or ""), "now": now}).mappings().first()
    if not row:
        seen = peek_action_token(connection, token, now)
        raise ValueError(seen["word"] if seen else "This link is not one BOSS sent")
    who = "email link"
    if row["action"] == "ack":
        return {"action": "ack", **ack(connection, row["project_id"],
                                       alert_ids=[row["alert_id"]] if row["alert_id"] else [],
                                       incident_id=row["incident_id"], actor=who, via="email",
                                       now=now)}
    # resolve: the person says it is over; the alert record keeps its evaluated state
    if row["incident_id"]:
        connection.execute(sql(
            "UPDATE incidents SET status = 'resolved', lifecycle = 'resolved', resolved_at = :now, "
            " last_seen = :now WHERE id = :i AND status NOT IN ('resolved', 'dismissed')"),
            {"i": row["incident_id"], "now": now})
    return {"action": "resolve", "incident_id": row["incident_id"]}


__all__ = ["IngestRefused", "SOURCE_KINDS", "accepted", "ack", "acked_now", "alert_labels",
           "audit", "authenticate", "create_source", "derive_token", "ensure_target_services",
           "find_or_create_service", "flap_state", "flap_update", "floor_store",
           "inhibit_rules", "inhibited_by", "labels_hash", "matches", "mint_action_token",
           "note_data", "parse_matchers", "parse_rrule", "receipt", "refuse", "rotate_source",
           "service", "sign", "sources", "stamp", "subject_of", "suppressors", "transition",
           "use_action_token", "validate_matchers", "window_active", "write_points"]
