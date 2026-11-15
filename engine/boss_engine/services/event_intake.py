"""EVENT INTAKE (P9a-1, BOSSAPP-1093; boss-eventing-lld.md §2-§4, §7.3, §10, §13).

(`services/intake.py` is the older command-intake service, so this module is `event_intake`.)

THE FRONT DOOR does four things and nothing more (`receive`): verify the raw bytes with the
source's own proof, insert ONE `intake_events` row (`ON CONFLICT DO NOTHING` on the source's own
delivery id), commit, answer the source's success code. Postgres announces the row on
`boss_intake` at commit. Never a provider call, a model call or the router before the answer;
never a non-2xx for a duplicate, a paused connection or a storm (a source that retries on
non-2xx would hammer us, and Slack auto-disables an app at > 95 % failures); 503 only when the
database itself is down, the one case the source's retry is allowed to carry durability.

THE PROCESSOR (`process_due`) is a competing consumer: claim with `FOR UPDATE SKIP LOCKED` and a
60 s lease, parse in full, screen the outsider's text for prompt injection, drop our own bot's
echoes (loop layer 1) and anything that already hopped three times (layer 2), serialise events of
one conversation with an advisory lock, then hand the event to the ROUTER
(services/intake_router.route: filters, addressing, identity, thread keys, guards — P9a-2) and
fire what it returns through `agent_triggers.fire`. A route that DEFERS the event (a rate limit,
quiet hours, a person without a grant yet) parks it `failed` with `next_attempt_at` and no
attempt counted; digest mode records it `digest`.
A failure retries with backoff (1 s … 2 h, 8 attempts) and then parks the event `dead` with its
reason; a person replays it (`replay`): `reprocess` fires only routes that have not admitted a run
for this event (the fires table's dedupe key), `force` with a typed reason runs it again.

Secrets (signing secrets, API tokens) go through secrets_service.put_secret; a row holds only
references, and nothing here ever returns, logs or echoes one. A minted secret is in the answer
of `create_connection` / `rotate_secret` ONCE.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets as _secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.intake import REGISTRY
from ..connectors.intake.base import screen_event
from ..connectors.intake.contract import (
    ACK_DUPLICATE,
    ACK_HANDSHAKE,
    ACK_NOT_FOUND,
    ACK_PAUSED,
    ACK_RECEIVED,
    ACK_REFUSED,
    ACK_SHED,
    ACK_TOO_LARGE,
    ACK_UNAVAILABLE,
    EVENT_STATUSES,
    MAX_BODY_BYTES,
    MAX_RAW_BYTES,
    MODES,
    TERMINAL_STATUSES,
    CapabilityNotSupported,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    RawRequest,
    RawResponse,
    StoredRaw,
    VerifyRefused,
    kept_headers,
    lower_headers,
)
from . import intake_router
from .core import emit_event, make_id

logger = logging.getLogger("boss.intake")

#: The public path the console shows: /v1/intake/<kind>/<path_token>.
PATH_PREFIX = "/v1/intake/"
#: A claimed row is this process's for this long; a crashed processor's rows are re-claimed after.
LEASE_SECONDS = 60
#: After this many failed attempts an event is dead (§4.3).
MAX_ATTEMPTS = 8
BACKOFF_SECONDS = (1, 5, 30, 120, 600, 1800, 3600, 7200)
#: Per-connection inbound limits (§7.3). Beyond `hard_cap_per_min` events are kept as `shed`.
DEFAULT_LIMITS: dict[str, int] = {"rps": 20, "burst": 100, "hard_cap_per_min": 3000,
                                  "max_backlog": 10000}
#: Retention (§4.5, §15 D9).
RAW_RETENTION_DAYS = 14
ENVELOPE_RETENTION_DAYS = 30
DEAD_RETENTION_DAYS = 90
#: Refusals (bad signatures) are counted always and recorded as events at most this often.
REFUSAL_SAMPLE_SECONDS = 60
#: Loop prevention, layer 2: an event that already hopped this many agents is dropped.
MAX_HOPS = 3
#: After a rotation the previous signing secret still verifies for this long.
ROTATION_GRACE_SECONDS = 600
#: A `shed` row keeps its raw body only up to this size (the hash always stays).
SHED_RAW_BYTES = 16 * 1024
#: The processor's safety tick when a NOTIFY is lost (§13).
PROCESSOR_TICK_SECONDS = 2.0
PROCESSOR_BATCH = 50
SERVICE_KEY = "intake_processor"
CONNECTION_STATUSES = ("active", "paused", "disabled", "auth_failed", "rate_limited")
HOLDER = f"{socket.gethostname()[:40]}:{_secrets.token_hex(3)}"

#: A test seam: called with the new row id after the insert and before the commit. A hook that
#: raises stands in for "the API died after INSERT, before COMMIT" (LLD §14 P9a-T4).
_BEFORE_COMMIT: Optional[Callable[[str], None]] = None


class IntakeServiceError(ValueError):
    def __init__(self, message: str, *, status: int = 422, code: str = "invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _settings():
    from ..config import get_settings

    return get_settings()


def inbound_path(kind: str, path_token: str) -> str:
    return f"{PATH_PREFIX}{kind}/{path_token}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def _adapter(kind: str):
    adapter = REGISTRY.get(kind)
    if adapter is None:
        raise IntakeServiceError(f"unknown source kind {kind!r}; one of "
                                 f"{', '.join(REGISTRY.adapters)}", status=404, code="unknown_kind")
    return adapter


def _secret_name(connection_id: str, field_name: str, version: int = 0) -> str:
    """The backend key of one secret. A rotation writes a NEW name (`_v<n>`): put_secret on the
    same name overwrites the value in place, which would end the previous secret's grace at once."""
    return f"intake_{connection_id}_{field_name}" + (f"_v{version}" if version else "")


def _store_secret(connection: Connection, project_id: str, connection_id: str, field_name: str,
                  value: str, actor: str, *, version: int = 0) -> str:
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    settings = _settings()
    stored = put_secret(connection, settings, project_id=project_id,
                        name=_secret_name(connection_id, field_name, version), value=value,
                        secret_kind="api_token", backend=default_secret_backend(settings),
                        actor=actor)
    return str(stored["secret_ref_id"])


def _limits(raw: Any) -> dict[str, int]:
    out = dict(DEFAULT_LIMITS)
    for key, value in dict(_json(raw, {}) or {}).items():
        if key in out and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[key] = value
    return out


def _mint(field_name: str) -> str:
    """A signing secret in the Standard Webhooks form — `whsec_` + base64 of 32 random bytes —
    so a compliant sender library decodes it as the key (the BOSS v1 scheme uses the same string
    as its key bytes); other secret fields are plain url-safe tokens."""
    if field_name.endswith("secret"):
        return "whsec_" + base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    return _secrets.token_urlsafe(32)


def create_connection(connection: Connection, *, project_id: str, kind: str, actor: str,
                      name: str = "", mode: str = "push",
                      config: Optional[Mapping[str, Any]] = None,
                      secrets: Optional[Mapping[str, str]] = None,
                      limits: Optional[Mapping[str, Any]] = None,
                      org_id: Optional[str] = None) -> dict:
    """A new connection. Secret fields the caller did not supply are MINTED when the source
    signs with a BOSS-issued secret (a `signing_secret`), and shown ONCE in the answer under
    `secrets_shown_once`; a required source-issued secret (GitHub's webhook secret) must be given.
    Non-secret config keys are kept as given; secret values never land in `config`."""
    adapter = _adapter(kind)
    manifest = adapter.manifest
    if mode not in MODES or mode not in manifest.modes:
        raise IntakeServiceError(f"{kind} supports modes {', '.join(manifest.modes)}, not {mode!r}.")
    cfg: dict[str, Any] = {}
    given_secrets = {k: str(v) for k, v in dict(secrets or {}).items() if v not in (None, "")}
    secret_fields = {f["name"] for f in manifest.auth_fields if f.get("secret")}
    for key, value in dict(config or {}).items():
        if key in secret_fields:
            given_secrets.setdefault(key, str(value))      # a secret typed into config is a secret
            continue
        cfg[str(key)] = value
    for f in manifest.auth_fields:
        if not f.get("secret") and f.get("required", True) and cfg.get(f["name"]) in (None, ""):
            raise IntakeServiceError(f"{f['label']} is required.", code="missing_field")
    connection_id = make_id("icn")
    shown: dict[str, str] = {}
    refs: dict[str, str] = {}
    for f in manifest.auth_fields:
        if not f.get("secret"):
            continue
        fname = f["name"]
        value = given_secrets.get(fname)
        if not value and fname == "signing_secret":
            value = _mint(fname)
            shown[fname] = value
        if not value:
            if f.get("required", True):
                raise IntakeServiceError(f"{f['label']} is required.", code="missing_secret")
            continue
        refs[fname] = _store_secret(connection, project_id, connection_id, fname, value, actor)
    path_token = _secrets.token_urlsafe(24)
    link = _standing_link(connection)
    connection.execute(text("""
        INSERT INTO intake_connections (id, project_id, org_id, kind, name, mode, config,
               secret_refs, path_token, status, limits, created_by, on_behalf_of, grant_id)
        VALUES (:id, :p, :org, :k, :n, :m, CAST(:c AS JSONB), CAST(:s AS JSONB), :tok, 'active',
                CAST(:l AS JSONB), :by, :obo, :grant)"""),
        {"id": connection_id, "p": project_id, "org": org_id, "k": adapter.key, "n": name[:120],
         "m": mode, "c": json.dumps(cfg, default=str), "s": json.dumps(refs), "tok": path_token,
         "l": json.dumps(_limits(limits)), "by": actor,
         "obo": (link or {}).get("on_behalf_of"), "grant": (link or {}).get("grant_id")})
    emit_event(connection, project_id, "intake.connection_created", actor, "intake_connection",
               connection_id, {"kind": adapter.key, "mode": mode, "name": name[:120],
                               "secret_fields": sorted(refs)})
    out = describe_connection(connection, get_connection(connection, connection_id) or {})
    if shown:
        out["secrets_shown_once"] = shown
        out["secret"] = next(iter(shown.values()))
        out["secret_shown_once"] = True
    return out


def _standing_link(connection: Connection) -> Optional[dict]:
    """The creating person's standing grant, as agent_triggers links it (the owner a service
    event acts for, re-checked at every firing by the route it fires)."""
    try:
        from ..identity import grants, lineage

        ctx = lineage.current_context()
        if ctx is None or not ctx.is_human_session:
            return None
        standing = [g for g in grants.for_person(ctx.principal.id, db=connection)
                    if not g.task_run_id]
        if not standing:
            return None
        g = sorted(standing, key=lambda x: (x.expires_at is None, x.expires_at))[-1]
        return {"on_behalf_of": ctx.principal.id, "grant_id": g.id}
    except Exception:  # noqa: BLE001 — unlinked is the rule, never a failed create
        return None


def get_connection(connection: Connection, connection_id: str, *,
                   project_id: Optional[str] = None, lock: bool = False) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT * FROM intake_connections WHERE id = :c"
        + (" AND project_id = :p" if project_id else "") + (" FOR UPDATE" if lock else "")),
        {"c": connection_id, "p": project_id}).mappings().first()
    return dict(row) if row else None


def connection_by_token(connection: Connection, kind: str, path_token: str) -> Optional[dict]:
    token = str(path_token or "").strip()
    if not token or len(token) > 128:
        return None
    row = connection.execute(text(
        "SELECT * FROM intake_connections WHERE path_token = :t AND kind = :k"),
        {"t": token, "k": (kind or "").strip().lower()}).mappings().first()
    return dict(row) if row else None


def list_connections(connection: Connection, project_id: str) -> list[dict]:
    rows = connection.execute(text(
        "SELECT * FROM intake_connections WHERE project_id = :p ORDER BY created_at, id"),
        {"p": project_id}).mappings().all()
    counts = {r["connection_id"]: dict(r) for r in connection.execute(text("""
        SELECT connection_id,
               count(*) FILTER (WHERE received_at > now() - interval '1 hour') AS last_hour,
               count(*) FILTER (WHERE status IN ('received', 'processing', 'failed')) AS backlog,
               count(*) FILTER (WHERE status = 'dead') AS dead,
               max(received_at) AS last_event_at
          FROM intake_events WHERE project_id = :p GROUP BY connection_id"""),
        {"p": project_id}).mappings().all()}
    rules = {r["connection_id"]: int(r["n"]) for r in connection.execute(text(
        "SELECT connection_id, count(*) AS n FROM agent_triggers WHERE kind = 'intake' "
        "AND project_id = :p AND connection_id IS NOT NULL GROUP BY connection_id"),
        {"p": project_id}).mappings().all()}
    return [describe_connection(connection, dict(r), counts=counts.get(r["id"]) or {},
                                rules=rules.get(r["id"], 0)) for r in rows]


def describe_connection(connection: Connection, row: Mapping[str, Any], *,
                        counts: Optional[Mapping[str, Any]] = None,
                        rules: Optional[int] = None) -> dict:
    """The console's row (E1): what it is, whether it is healthy, how busy. Never a secret."""
    if not row:
        return {}
    adapter = REGISTRY.get(str(row["kind"]))
    manifest = adapter.manifest if adapter else None
    refs = _json(row.get("secret_refs"), {}) or {}
    health = _json(row.get("health"), {}) or {}
    out = {
        "id": row["id"], "project_id": row.get("project_id"), "kind": row["kind"],
        "label": manifest.label if manifest else row["kind"],
        "icon": manifest.icon if manifest else row["kind"],
        "name": row.get("name") or "", "mode": row["mode"], "status": row["status"],
        "config": _json(row.get("config"), {}) or {},
        "secret_fields": sorted(k for k in refs if not k.endswith("_previous")),   # names only
        "url_path": inbound_path(str(row["kind"]), str(row["path_token"])),
        "limits": _limits(row.get("limits")), "health": health,
        "rules": rules, "created_by": row.get("created_by"),
        "runs_as": ({"on_behalf_of": row.get("on_behalf_of"), "grant_id": row.get("grant_id")}
                    if row.get("on_behalf_of") else None),
        "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
    }
    if counts is not None:
        out["last_hour"] = int(counts.get("last_hour") or 0)
        out["backlog"] = int(counts.get("backlog") or 0)
        out["dead"] = int(counts.get("dead") or 0)
        out["last_event_at"] = _iso(counts.get("last_event_at"))
    return out


def update_connection(connection: Connection, *, project_id: str, connection_id: str, actor: str,
                      name: Optional[str] = None, config: Optional[Mapping[str, Any]] = None,
                      limits: Optional[Mapping[str, Any]] = None,
                      secrets: Optional[Mapping[str, str]] = None) -> dict:
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    adapter = _adapter(str(row["kind"]))
    secret_fields = {f["name"] for f in adapter.manifest.auth_fields if f.get("secret")}
    cfg = dict(_json(row.get("config"), {}) or {})
    given_secrets = {k: str(v) for k, v in dict(secrets or {}).items() if v not in (None, "")}
    if config is not None:
        for key, value in dict(config).items():
            if key in secret_fields:
                given_secrets.setdefault(key, str(value))
            elif value is None:
                cfg.pop(str(key), None)
            else:
                cfg[str(key)] = value
    refs = dict(_json(row.get("secret_refs"), {}) or {})
    for fname, value in given_secrets.items():
        if fname not in secret_fields:
            raise IntakeServiceError(f"{fname} is not a secret field of {row['kind']}.")
        refs[fname] = _store_secret(connection, project_id, connection_id, fname, value, actor)
    connection.execute(text("""
        UPDATE intake_connections SET name = COALESCE(:n, name), config = CAST(:c AS JSONB),
               secret_refs = CAST(:s AS JSONB), limits = CAST(:l AS JSONB), updated_at = now()
         WHERE id = :id"""),
        {"n": name[:120] if name is not None else None, "c": json.dumps(cfg, default=str),
         "s": json.dumps(refs),
         "l": json.dumps(_limits(limits) if limits is not None else _limits(row.get("limits"))),
         "id": connection_id})
    emit_event(connection, project_id, "intake.connection_updated", actor, "intake_connection",
               connection_id, {"kind": row["kind"], "secret_fields_rotated": sorted(given_secrets)})
    return describe_connection(connection, get_connection(connection, connection_id) or {})


def set_connection_status(connection: Connection, *, project_id: str, connection_id: str,
                          actor: str, status: str, reason: str = "") -> dict:
    if status not in CONNECTION_STATUSES:
        raise IntakeServiceError(f"status must be one of {', '.join(CONNECTION_STATUSES)}.")
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    health = dict(_json(row.get("health"), {}) or {})
    health["status_reason"] = reason[:300] or None
    health["status_changed_at"] = _now().isoformat()
    connection.execute(text(
        "UPDATE intake_connections SET status = :s, health = CAST(:h AS JSONB), updated_at = now() "
        "WHERE id = :id"), {"s": status, "h": json.dumps(health), "id": connection_id})
    emit_event(connection, project_id, f"intake.connection_{status}", actor, "intake_connection",
               connection_id, {"reason": reason[:300] or None})
    return describe_connection(connection, get_connection(connection, connection_id) or {})


def delete_connection(connection: Connection, *, project_id: str, connection_id: str,
                      actor: str) -> bool:
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        return False
    connection.execute(text("DELETE FROM intake_connections WHERE id = :id"), {"id": connection_id})
    emit_event(connection, project_id, "intake.connection_deleted", actor, "intake_connection",
               connection_id, {"kind": row["kind"]})
    return True


def rotate_secret(connection: Connection, *, project_id: str, connection_id: str, actor: str,
                  field_name: Optional[str] = None) -> dict:
    """A new BOSS-minted secret for a signing field, shown once; the old one stops verifying at
    once. Source-issued secrets (GitHub's) are set with `update_connection(secrets=...)`."""
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    adapter = _adapter(str(row["kind"]))
    secret_fields = [f["name"] for f in adapter.manifest.auth_fields if f.get("secret")]
    fname = field_name or ("signing_secret" if "signing_secret" in secret_fields
                           else (secret_fields[0] if secret_fields else ""))
    if fname not in secret_fields:
        raise IntakeServiceError(f"{row['kind']} has no secret field {fname!r}.")
    value = _mint(fname)
    refs = dict(_json(row.get("secret_refs"), {}) or {})
    cfg = dict(_json(row.get("config"), {}) or {})
    # The old secret keeps verifying for a short grace (the sender is re-pointed by a person,
    # not atomically); after `<field>_previous_until` only the new one does.
    version = int(cfg.get(f"{fname}_version") or 0) + 1
    if refs.get(fname):
        refs[f"{fname}_previous"] = refs[fname]
        cfg[f"{fname}_previous_until"] = (_now() + timedelta(seconds=ROTATION_GRACE_SECONDS)
                                          ).isoformat()
    cfg[f"{fname}_version"] = version
    refs[fname] = _store_secret(connection, project_id, connection_id, fname, value, actor,
                                version=version)
    connection.execute(text(
        "UPDATE intake_connections SET secret_refs = CAST(:s AS JSONB), config = CAST(:c AS JSONB), "
        "updated_at = now() WHERE id = :id"),
        {"s": json.dumps(refs), "c": json.dumps(cfg, default=str), "id": connection_id})
    emit_event(connection, project_id, "intake.connection_secret_rotated", actor,
               "intake_connection", connection_id,
               {"field": fname, "grace_seconds": ROTATION_GRACE_SECONDS})
    out = describe_connection(connection, get_connection(connection, connection_id) or {})
    out.update({"secret": value, "secret_field": fname, "secret_shown_once": True,
                "secrets_shown_once": {fname: value}})
    return out


def test_connection(connection: Connection, settings: Any, *, project_id: str,
                    connection_id: str) -> dict:
    """Is the connection usable: its secrets resolve, the adapter's own health check (when it has
    one), and what the inbox says about it (last event, refusals, backlog, dead letters)."""
    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    adapter = _adapter(str(row["kind"]))
    conn = to_intake_connection(connection, settings, row)
    checks: dict[str, Any] = {}
    refs = _json(row.get("secret_refs"), {}) or {}
    for fname in refs:
        try:
            checks[f"secret:{fname}"] = "ok" if conn.secret(fname) else "empty"
        except Exception as error:  # noqa: BLE001 — the reason, never the value
            checks[f"secret:{fname}"] = f"unresolvable ({type(error).__name__})"
    if adapter.can("health"):
        try:
            got = adapter.health(conn)
            checks["adapter"] = {"ok": got.ok, "detail": got.detail, **dict(got.checks)}
        except Exception as error:  # noqa: BLE001
            checks["adapter"] = {"ok": False, "detail": f"{type(error).__name__}: {error}"[:300]}
    stats = connection.execute(text("""
        SELECT count(*) FILTER (WHERE received_at > now() - interval '1 hour') AS last_hour,
               count(*) FILTER (WHERE status IN ('received', 'processing', 'failed')) AS backlog,
               count(*) FILTER (WHERE status = 'dead') AS dead,
               count(*) FILTER (WHERE status = 'shed') AS shed,
               max(received_at) AS last_event_at
          FROM intake_events WHERE connection_id = :c"""), {"c": connection_id}).mappings().first()
    health = dict(_json(row.get("health"), {}) or {})
    adapter_ok = checks["adapter"]["ok"] if isinstance(checks.get("adapter"), dict) else True
    ok = all(v == "ok" for k, v in checks.items() if k.startswith("secret:")) and bool(adapter_ok)
    health.update({"last_test_at": _now().isoformat(), "last_test_ok": ok})
    connection.execute(text(
        "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = now() WHERE id = :id"),
        {"h": json.dumps(health, default=str), "id": connection_id})
    return {"ok": ok, "checks": checks, "url_path": inbound_path(str(row["kind"]),
                                                                 str(row["path_token"])),
            "last_hour": int(stats["last_hour"] or 0), "backlog": int(stats["backlog"] or 0),
            "dead": int(stats["dead"] or 0), "shed": int(stats["shed"] or 0),
            "last_event_at": _iso(stats["last_event_at"]),
            "refusals": int(health.get("refusals") or 0),
            "last_refusal": health.get("last_refusal")}


def to_intake_connection(connection: Connection, settings: Any, row: Mapping[str, Any]
                         ) -> IntakeConnection:
    """The adapter's view of a row; secrets resolve from the backend when asked, never before."""
    refs = _json(row.get("secret_refs"), {}) or {}
    project_id = row.get("project_id")

    def resolve(name: str) -> Optional[str]:
        ref = refs.get(name)
        if not ref:
            return None
        from .secret_backends import resolve_secret_ref

        return resolve_secret_ref(connection, ref, settings, project_id=project_id)

    return IntakeConnection(
        str(row["id"]), str(project_id or ""), str(row["kind"]), mode=str(row["mode"]),
        config=_json(row.get("config"), {}) or {}, secret_resolver=resolve,
        org_id=row.get("org_id"), name=str(row.get("name") or ""), status=str(row["status"]),
        limits=_limits(row.get("limits")), app_settings=settings, db=connection)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The front door
# ═══════════════════════════════════════════════════════════════════════════════════════════════

class _Buckets:
    """Per-connection token buckets and per-minute counters, in memory per API instance (§7.3).
    `admit` says whether this event is over the connection's steady rate (a health note) and
    whether it is beyond the hard cap (kept as `shed`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, float]] = {}

    def admit(self, connection_id: str, limits: Mapping[str, int], now: float) -> tuple[bool, bool]:
        rps, burst, cap = float(limits["rps"]), float(limits["burst"]), int(limits["hard_cap_per_min"])
        with self._lock:
            s = self._state.get(connection_id)
            if s is None:
                s = {"tokens": burst, "at": now, "minute": now // 60, "count": 0.0}
                self._state[connection_id] = s
            s["tokens"] = min(burst, s["tokens"] + (now - s["at"]) * rps)
            s["at"] = now
            if now // 60 != s["minute"]:
                s["minute"], s["count"] = now // 60, 0.0
            s["count"] += 1
            over_rate = s["tokens"] < 1.0
            if not over_rate:
                s["tokens"] -= 1.0
            return over_rate, s["count"] > cap

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


BUCKETS = _Buckets()


class _Refusals:
    """Bad signatures are counted in memory per connection and flushed to the row (one UPDATE
    plus one sampled audit event) at most once a minute per connection, and on every processor
    tick — never one write per refused request (an unauthenticated flood must not churn the
    row or the WAL)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._flushed_at: dict[str, float] = {}

    def count(self, row: Mapping[str, Any], reason: str, now: float) -> bool:
        """Record one refusal; True when this connection is due a flush."""
        with self._lock:
            entry = self._pending.setdefault(str(row["id"]), {
                "count": 0, "reason": reason, "project_id": row.get("project_id"),
                "kind": row.get("kind")})
            entry["count"] += 1
            entry["reason"] = reason
            entry["at"] = now
            return self._flushed_at.get(str(row["id"]), 0.0) + REFUSAL_SAMPLE_SECONDS <= now

    def pending(self, connection_id: str) -> int:
        with self._lock:
            return int((self._pending.get(connection_id) or {}).get("count") or 0)

    def take(self, connection_id: Optional[str], now: float, *, force: bool = False
             ) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            ids = [connection_id] if connection_id else list(self._pending)
            out = []
            for cid in ids:
                entry = self._pending.get(cid)
                if not entry:
                    continue
                if not force and self._flushed_at.get(cid, 0.0) + REFUSAL_SAMPLE_SECONDS > now:
                    continue
                out.append((cid, self._pending.pop(cid)))
                self._flushed_at[cid] = now
            return out

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._flushed_at.clear()


REFUSALS = _Refusals()


def _ack(adapter: Any, conn: Optional[IntakeConnection], req: RawRequest, outcome: str,
         **extra: Any) -> dict:
    try:
        response = adapter.ack(conn, req, outcome) if conn is not None else RawResponse(
            status=202, body={"status": outcome})
    except Exception:  # noqa: BLE001 — an adapter's ack bug never turns a stored event into a 5xx
        response = RawResponse(status=202, body={"status": outcome})
    body = response.body if isinstance(response.body, dict) else {"status": outcome}
    return {"outcome": outcome, "http_status": response.status, "body": {**body, **extra},
            "headers": dict(response.headers)}


def _refuse(status: int, outcome: str, reason: str, **extra: Any) -> dict:
    return {"outcome": outcome, "http_status": status,
            "body": {"status": outcome, "reason": reason, **extra}, "headers": {}}


def flush_refusals(engine: Any, *, connection_id: Optional[str] = None, now: Optional[float] = None,
                   force: bool = False) -> int:
    """Write the pending refusal counts: health.refusals += n, last reason, and ONE sampled
    `intake.verify_refused` event per flush. Returns how many connections were flushed."""
    from ..db import begin

    now_s = time.time() if now is None else now
    taken = REFUSALS.take(connection_id, now_s, force=force)
    if not taken:
        return 0
    try:
        with begin(engine) as c:
            for cid, entry in taken:
                c.execute(text("""
                    UPDATE intake_connections
                       SET health = health || jsonb_build_object(
                               'refusals', COALESCE((health->>'refusals')::int, 0) + :n,
                               'last_refusal', CAST(:r AS TEXT),
                               'last_refusal_at', CAST(:at AS TEXT)),
                           updated_at = now()
                     WHERE id = :id"""),
                    {"n": int(entry["count"]), "r": entry["reason"],
                     "at": datetime.fromtimestamp(entry.get("at") or now_s, tz=timezone.utc
                                                  ).isoformat(), "id": cid})
                if entry.get("project_id"):
                    emit_event(c, str(entry["project_id"]), "intake.verify_refused", "intake",
                               "intake_connection", cid,
                               {"reason": entry["reason"], "kind": entry.get("kind"),
                                "count": int(entry["count"])})
    except Exception:  # noqa: BLE001 — the refusal is the answer; the count is best effort
        logger.debug("intake refusals not flushed", exc_info=True)
    return len(taken)


def _count_refusal(engine: Any, row: Mapping[str, Any], reason: str, now: float) -> None:
    if REFUSALS.count(row, reason, now):
        flush_refusals(engine, connection_id=str(row["id"]), now=now)


def _db_down(error: Exception) -> bool:
    if isinstance(error, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.DisconnectionError)):
        return True
    if isinstance(error, sa_exc.DBAPIError) and getattr(error, "connection_invalidated", False):
        return True
    name = type(error).__name__
    return name in ("OperationalError", "InterfaceError", "ConnectionDoesNotExist")


def receive(engine: Any, kind: str, path_token: str, request: RawRequest, *,
            settings: Any = None, now: Optional[float] = None) -> dict:
    """ONE delivery at the front door. Returns {outcome, http_status, body, headers, event_id?}.
    Verified + inserted + COMMITTED before any 2xx; a duplicate, a paused connection and a
    storm all answer the source's success code; 503 only when the database is down (§3.5)."""
    from ..db import begin

    now_s = time.time() if now is None else now
    settings = settings or _settings()
    adapter = REGISTRY.get(kind)
    if adapter is None:
        return _refuse(404, ACK_NOT_FOUND, "unknown source kind")
    if len(request.body) > MAX_BODY_BYTES:
        return _refuse(413, ACK_TOO_LARGE, "body too large")
    # Step 1: find the connection, verify on the raw bytes. Nothing is stored here.
    try:
        with begin(engine) as c:
            row = connection_by_token(c, kind, path_token)
            if row is None:
                return _refuse(404, ACK_NOT_FOUND, "unknown connection")
            conn = to_intake_connection(c, settings, row)
            if request.method.upper() == "OPTIONS":
                shake = adapter.handshake(conn, request) if adapter.can("handshake") else None
                if shake is None:
                    return _refuse(405, ACK_REFUSED, "method not allowed")
                return {"outcome": ACK_HANDSHAKE, "http_status": shake.status, "body": shake.body,
                        "headers": dict(shake.headers)}
            try:
                verified = adapter.verify(conn, request, now_s)
            except VerifyRefused as refused:
                verified, reason = None, refused.reason
    except Exception as error:  # noqa: BLE001
        if _db_down(error):
            logger.warning("intake: database unavailable at verify", exc_info=True)
            return _refuse(503, ACK_UNAVAILABLE, "database unavailable")
        raise
    if verified is None:
        _count_refusal(engine, row, reason, now_s)
        return _refuse(401, ACK_REFUSED, reason)
    # Step 2: verified. A handshake (GitHub ping, Slack url_verification) stores nothing; a
    # paused connection stores nothing and still gets a 2xx; everything else is ONE insert.
    try:
        with begin(engine) as c:
            conn = to_intake_connection(c, settings, row)
            if adapter.can("handshake"):
                shake = adapter.handshake(conn, request)
                if shake is not None:
                    return {"outcome": ACK_HANDSHAKE, "http_status": shake.status,
                            "body": shake.body, "headers": dict(shake.headers)}
            if row["status"] != "active":
                key = "dropped_while_" + str(row["status"])
                c.execute(text("""
                    UPDATE intake_connections SET health = health || CAST(:h AS JSONB)
                     WHERE id = :id"""),
                    {"h": json.dumps({key: int((_json(row.get("health"), {}) or {}).get(key)
                                                or 0) + 1}), "id": row["id"]})
                return _ack(adapter, conn, request, ACK_PAUSED)
            dedupe_key = str(adapter.dedupe_key(conn, request))[:500]
            limits = _limits(row.get("limits"))
            over_rate, shed = BUCKETS.admit(str(row["id"]), limits, now_s)
            if over_rate and not shed:
                backlog = int(c.execute(text(
                    "SELECT count(*) FROM intake_events WHERE connection_id = :c "
                    "AND status IN ('received', 'processing', 'failed')"),
                    {"c": row["id"]}).scalar() or 0)
                shed = backlog >= int(limits["max_backlog"])
            retry = verified.retry or _retry_signal(adapter, request)
            event_id = make_id("iev")
            received = _now()
            inserted = c.execute(text("""
                INSERT INTO intake_events (id, connection_id, project_id, dedupe_key,
                       source_event_id, status, raw, raw_sha256, headers, retry_count_seen,
                       trace_id, verify_scheme, received_at, raw_expires_at, expires_at)
                VALUES (:id, :c, :p, :dk, :sid, :st, :raw, :sha, CAST(:h AS JSONB), :retry, :tr,
                        :vs, :at, :rexp, :exp)
                ON CONFLICT (connection_id, dedupe_key) DO NOTHING RETURNING id"""),
                {"id": event_id, "c": row["id"], "p": row.get("project_id"), "dk": dedupe_key,
                 "sid": dedupe_key.split(":", 1)[-1][:200],
                 "st": ACK_SHED if shed else ACK_RECEIVED,
                 "raw": request.body if len(request.body) <= (SHED_RAW_BYTES if shed
                                                             else MAX_RAW_BYTES) else None,
                 "sha": request.body_sha256(), "h": json.dumps(kept_headers(request.headers)),
                 "retry": int((retry or {}).get("num") or 0),
                 "tr": (lower_headers(request.headers).get("traceparent") or make_id("itr"))[:120],
                 "vs": verified.scheme, "at": received,
                 "rexp": received + timedelta(days=RAW_RETENTION_DAYS),
                 "exp": received + timedelta(days=ENVELOPE_RETENTION_DAYS)}).scalar()
            if inserted is None:
                c.execute(text("""
                    UPDATE intake_events SET duplicates = duplicates + 1,
                           retry_count_seen = GREATEST(retry_count_seen, :retry)
                     WHERE connection_id = :c AND dedupe_key = :dk"""),
                    {"retry": int((retry or {}).get("num") or 0), "c": row["id"], "dk": dedupe_key})
                return _ack(adapter, conn, request, ACK_DUPLICATE, dedupe_key=dedupe_key)
            if _BEFORE_COMMIT is not None:
                _BEFORE_COMMIT(event_id)
            outcome = ACK_SHED if shed else ACK_RECEIVED
            ack = _ack(adapter, conn, request, outcome, event_id=event_id)
        ack["event_id"] = event_id
        ack["shed"] = shed
        return ack
    except Exception as error:  # noqa: BLE001
        if _db_down(error):
            logger.warning("intake: database unavailable at insert", exc_info=True)
            return _refuse(503, ACK_UNAVAILABLE, "database unavailable")
        raise


def _retry_signal(adapter: Any, request: RawRequest) -> Optional[dict]:
    """{num, reason} from the source's redelivery headers the manifest names (X-Slack-Retry-*)."""
    names = list((getattr(adapter.manifest, "ack", None) or {}).get("retry_signal_headers") or [])
    if not names:
        return None
    h = lower_headers(request.headers)
    num = next((h[n] for n in names if n in h and h[n].isdigit()), None)
    reason = next((h[n] for n in names if n in h and not h[n].isdigit()), None)
    if num is None and reason is None:
        return None
    return {"num": int(num) if num is not None else 1, "reason": reason}


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The processor
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def backoff_seconds(attempts: int) -> int:
    return BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]


def claim(connection: Connection, *, limit: int = PROCESSOR_BATCH, holder: str = HOLDER,
          now: Optional[datetime] = None) -> list[dict]:
    """Claim due rows: received or failed and due, or `processing` whose lease expired (its
    processor died), in live (not archived) projects. SKIP LOCKED so two API processes never take
    one row; a crashed processor's rows come back after LEASE_SECONDS (attempts counts the
    retry). Round-robin across connections (one stormy source cannot starve the others, NFR-7).

    ORDER. Within one claim, a connection's rows come in received order. ACROSS claims and
    processors the only promise is serialisation: `process_one` holds a transaction-scoped
    advisory lock on (connection, conversation_key) while it fires, so two events of one
    conversation are never fired at the same time, and an event is DEFERRED behind an earlier
    event of the same conversation that is still retrying (`failed`). Two events of one
    conversation claimed by two processors in the same tick may fire in either order; strict
    per-conversation order is the thread key (P9a-2): `thread_runs.admit` serialises runs on
    the thread in admit order, and the agent sees every earlier turn in the thread's history."""
    from . import projects_archive

    now = now or _now()
    rows = connection.execute(text(f"""
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY connection_id ORDER BY received_at, id) AS rn,
                   received_at
              FROM intake_events
             WHERE ((status IN ('received', 'failed') AND next_attempt_at <= :now
                     AND (lease_until IS NULL OR lease_until < :now))
                    OR (status = 'processing' AND lease_until < :now))
               AND {projects_archive.live_sql('intake_events.project_id')}),
        due AS (
            SELECT e.id FROM intake_events e JOIN ranked r ON r.id = e.id
             ORDER BY r.rn, r.received_at, e.id
             LIMIT :n FOR UPDATE OF e SKIP LOCKED)
        UPDATE intake_events e
           SET status = 'processing', lease_until = :lease, claimed_by = :who,
               attempts = e.attempts + 1
          FROM due WHERE e.id = due.id
        RETURNING e.*"""),
        {"now": now, "lease": now + timedelta(seconds=LEASE_SECONDS), "who": holder[:80],
         "n": int(limit)}).mappings().all()
    # RETURNING does not keep the CTE's ORDER BY (an UPDATE … FROM answers in join order), so
    # the batch is put back in the order the query chose: round-robin rank across connections,
    # then received order within one. The processor walks the list as it is (P9a-2 T5).
    out = sorted((dict(r) for r in rows), key=lambda r: (r["received_at"], r["id"]))
    rank: dict[str, int] = {}
    for r in out:
        rank[r["connection_id"]] = rank.get(r["connection_id"], 0) + 1
        r["_rn"] = rank[r["connection_id"]]
    out.sort(key=lambda r: (r.pop("_rn"), r["received_at"], r["id"]))
    return out


def _finish(connection: Connection, event_id: str, status: str, *, envelope: Any = None,
            outcome: Any = None, error: Optional[str] = None, now: Optional[datetime] = None,
            next_attempt_at: Optional[datetime] = None, fields: Optional[Mapping[str, Any]] = None
            ) -> None:
    if status not in EVENT_STATUSES:
        raise ValueError(f"not an intake status: {status}")
    now = now or _now()
    f = dict(fields or {})
    connection.execute(text("""
        UPDATE intake_events
           SET status = :s, lease_until = NULL, last_error = :err,
               next_attempt_at = COALESCE(:nxt, next_attempt_at),
               processed_at = CASE WHEN :terminal THEN :now ELSE processed_at END,
               envelope = COALESCE(CAST(:env AS JSONB), envelope),
               outcome = CASE WHEN :has_outcome THEN CAST(:out AS JSONB) ELSE outcome END,
               type = COALESCE(:type, type), subject = COALESCE(:subject, subject),
               conversation_key = COALESCE(:ck, conversation_key),
               actor_kind = COALESCE(:ak, actor_kind),
               actor_external_id = COALESCE(:ax, actor_external_id),
               principal_id = COALESCE(:pid, principal_id),
               screen = COALESCE(CAST(:screen AS JSONB), screen),
               event_time = COALESCE(:et, event_time),
               expires_at = CASE WHEN :s IN ('dead', 'refused')
                                 THEN :now + make_interval(days => :dead_days) ELSE expires_at END
         WHERE id = :id"""),
        {"s": status, "err": (error or "")[:1000] or None, "nxt": next_attempt_at,
         "terminal": status in TERMINAL_STATUSES, "now": now,
         "env": json.dumps(envelope, default=str) if envelope is not None else None,
         "has_outcome": outcome is not None,
         "out": json.dumps(outcome, default=str) if outcome is not None else "[]",
         "type": f.get("type"), "subject": f.get("subject"), "ck": f.get("conversation_key"),
         "ak": f.get("actor_kind"), "ax": f.get("actor_external_id"),
         "pid": f.get("principal_id"),
         "screen": json.dumps(f["screen"], default=str) if f.get("screen") is not None else None,
         "et": f.get("event_time"), "dead_days": DEAD_RETENTION_DAYS, "id": event_id})


def _event_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _replay_mode(row: Mapping[str, Any]) -> tuple[str, int]:
    """(mode, n) of the latest replay marker on the row's outcome, else ('', 0)."""
    for entry in reversed(_json(row.get("outcome"), []) or []):
        if isinstance(entry, dict) and "replay" in entry:
            return str(entry.get("mode") or "reprocess"), int(entry["replay"])
    return "", 0


def process_one(engine: Any, settings: Any, row: Mapping[str, Any], *,
                now: Optional[datetime] = None) -> dict:
    """One claimed row, one transaction: parse → enrich → screen → loop filters → route → fire →
    status. Raises nothing: every failure is written on the row (failed with backoff, or dead)."""
    from ..db import begin

    now = now or _now()
    event_id = str(row["id"])
    attempts = int(row.get("attempts") or 1)
    parsed_fields: dict[str, Any] = {}
    parsed_envelope: Optional[dict] = None
    try:
        with begin(engine) as c:
            crow = get_connection(c, str(row["connection_id"]))
            if crow is None:
                _finish(c, event_id, "dead", error="connection deleted", now=now)
                return {"id": event_id, "status": "dead", "reason": "connection deleted"}
            adapter = _adapter(str(crow["kind"]))
            conn = to_intake_connection(c, settings, crow)
            if crow["status"] == "disabled":
                _finish(c, event_id, "refused", error="connection disabled", now=now)
                return {"id": event_id, "status": "refused", "reason": "connection disabled"}
            raw = row.get("raw")
            if raw is None:
                _finish(c, event_id, "dead", error="raw body expired (retention)", now=now)
                return {"id": event_id, "status": "dead", "reason": "raw expired"}
            stored = StoredRaw(body=bytes(raw), headers=_json(row.get("headers"), {}) or {},
                               dedupe_key=str(row["dedupe_key"]), received_at=row.get("received_at"),
                               event_row_id=event_id)
            events = adapter.parse(conn, stored)
            if not events:
                _finish(c, event_id, "ignored", now=now,
                        outcome=[{"reason": "the adapter ignored this delivery"}])
                return {"id": event_id, "status": "ignored"}
            replay_mode, replay_n = _replay_mode(row)
            force_n = replay_n if replay_mode == "force" else 0
            all_outcomes: list[dict] = []
            primary: Optional[IntakeEvent] = None
            primary_conversation: Optional[str] = None
            filtered_reason = ""
            for ev in events:
                ev.bossconnection = conn.id
                ev.bossdedupe = ev.bossdedupe or str(row["dedupe_key"])
                try:
                    ev = adapter.enrich(conn, ev)
                except Exception as error:  # noqa: BLE001 — enrichment is optional context
                    ev.data["enrich_error"] = f"{type(error).__name__}"
                ev = screen_event(conn, ev)
                conversation = None
                if adapter.can("conversation"):
                    try:
                        conversation = adapter.conversation_key(conn, ev)
                    except CapabilityNotSupported:
                        conversation = None
                if conversation and ev.bossconversation is None:
                    ev.bossconversation = {"key": conversation}
                if primary is None:
                    primary, primary_conversation = ev, conversation
                    parsed_fields = _fields_of(primary, conversation)
                    parsed_envelope = primary.to_dict()
                # Loop prevention (§7.1): our own echo, or an event that already hopped 3 times.
                if adapter.is_self(conn, ev):
                    filtered_reason = "our own bot wrote this (loop layer 1)"
                    all_outcomes.append({"filtered": filtered_reason, "event": ev.id})
                    continue
                hops = int((ev.bosscausality or {}).get("hops") or 0)
                if hops >= MAX_HOPS:
                    filtered_reason = f"caused by {hops} agent hops already (loop layer 2)"
                    all_outcomes.append({"filtered": filtered_reason, "event": ev.id})
                    emit_event(c, conn.project_id, "intake.loop_dropped", "intake", "intake_event",
                               event_id, {"hops": hops, "connection_id": conn.id})
                    continue
                if conversation:
                    # Per-conversation serialisation (§8.4): one conversation fires one event at
                    # a time; see `claim` for exactly what is and is not promised about order.
                    c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                              {"k": f"{conn.id}:{conversation}"})
                    earlier = c.execute(text("""
                        SELECT id, next_attempt_at FROM intake_events
                         WHERE connection_id = :c AND conversation_key = :k AND id <> :id
                           AND status = 'failed' AND received_at < :at
                         ORDER BY received_at LIMIT 1"""),
                        {"c": conn.id, "k": conversation, "id": event_id,
                         "at": row.get("received_at")}).mappings().first()
                    if earlier is not None:
                        # Never overtake an earlier event of the same conversation that is still
                        # retrying: wait until after its next attempt.
                        due = max(earlier["next_attempt_at"], now) + timedelta(seconds=1)
                        history = [m for m in (_json(row.get("outcome"), []) or [])
                                   if isinstance(m, dict) and "replay" in m]
                        _finish(c, event_id, "received", now=now, next_attempt_at=due,
                                envelope=parsed_envelope, fields=parsed_fields,
                                outcome=history + [{"deferred_behind": earlier["id"],
                                                    "until": due.isoformat()}])
                        c.execute(text("UPDATE intake_events SET attempts = attempts - 1 "
                                       "WHERE id = :id AND attempts > 0"), {"id": event_id})
                        return {"id": event_id, "status": "received",
                                "reason": f"deferred behind {earlier['id']}"}
                fires = intake_router.route(c, conn, ev, row, replay_n=force_n,
                                            settings=settings, now=now)
                caused_by = (ev.bosscausality or {}).get("deployment_id")
                if caused_by:
                    kept = []
                    for f in fires:
                        dep = c.execute(text(
                            "SELECT deployment_id FROM agent_triggers WHERE id = :t"),
                            {"t": f.trigger_id}).scalar()
                        if str(dep) == str(caused_by):
                            all_outcomes.append({"trigger_id": f.trigger_id,
                                                 "fire_outcome": "filtered",
                                                 "reason": "the same deployment caused it (loop)"})
                        else:
                            kept.append(f)
                    fires = kept
                all_outcomes.extend(intake_router.fire_all(c, fires))
            words = {o.get("fire_outcome") for o in all_outcomes if "fire_outcome" in o}
            if "failed" in words:
                raise RuntimeError("; ".join(str(o.get("reason")) for o in all_outcomes
                                             if o.get("fire_outcome") == "failed")[:500])
            fields: dict[str, Any] = {}
            envelope = None
            if primary is not None:
                fields = _fields_of(primary, primary_conversation)
                envelope = primary.to_dict()
                if len(events) > 1:
                    envelope["bossbatch"] = {"count": len(events), "ids": [e.id for e in events]}
            fields["principal_id"] = next((str(o["subject"]["principal_id"]) for o in all_outcomes
                                           if isinstance(o.get("subject"), dict)
                                           and o["subject"].get("principal_id")), None)
            # Replay markers (who replayed, why) stay on the row ahead of this pass's outcomes.
            history = [m for m in (_json(row.get("outcome"), []) or [])
                       if isinstance(m, dict) and "replay" in m]
            # DEFERRED (P9a-2, §6.1/§7): a guard or a missing grant holds the event until
            # `until`; it comes back through the inbox and the routes that already admitted a run
            # dedupe to `duplicate`. The attempt is not counted (a deferral is not a failure).
            held = [o for o in all_outcomes if o.get("fire_outcome") == "deferred"
                    and not o.get("digest") and o.get("until")]
            if held:
                until = min(_event_time(o["until"]) or now for o in held)
                _finish(c, event_id, "failed", envelope=envelope, outcome=history + all_outcomes,
                        now=now, fields=fields,
                        error="deferred: " + "; ".join(str(o.get("reason")) for o in held)[:800],
                        next_attempt_at=max(until, now + timedelta(seconds=1)))
                c.execute(text("UPDATE intake_events SET attempts = GREATEST(attempts - 1, 0) "
                               "WHERE id = :id"), {"id": event_id})
                return {"id": event_id, "status": "deferred", "outcome": all_outcomes,
                        "until": until.isoformat()}
            digest = any(o.get("digest") for o in all_outcomes)
            if words & {"admitted", "duplicate", "skipped"} and not digest:
                status = "routed"
            elif digest:
                status = "digest"
            elif words and words <= {"refused"}:
                status = "refused"
            elif words or filtered_reason:
                status = "filtered"
            else:
                status = "ignored"
                all_outcomes.append({"reason": "no route on this connection"})
            if status == "routed":
                emit_event(c, conn.project_id, "intake.event_routed", "intake", "intake_event",
                           event_id, {"connection_id": conn.id, "type": fields.get("type"),
                                      "runs": [o.get("run_id") for o in all_outcomes
                                               if o.get("run_id")]})
            _finish(c, event_id, status, envelope=envelope, outcome=history + all_outcomes,
                    now=now, fields=fields)
            return {"id": event_id, "status": status, "outcome": all_outcomes}
    except ParseRefused as refused:
        with begin(engine) as c:
            _dead(c, row, f"parse refused: {refused}", now=now)
        return {"id": event_id, "status": "dead", "reason": str(refused)}
    except Exception as error:  # noqa: BLE001 — written on the row, retried or dead
        word = f"{type(error).__name__}: {error}"[:500]
        logger.warning("intake: event %s failed attempt %s: %s", event_id, attempts, word)
        try:
            with begin(engine) as c:
                if attempts >= MAX_ATTEMPTS:
                    _dead(c, row, word, now=now, fields=parsed_fields, envelope=parsed_envelope)
                    return {"id": event_id, "status": "dead", "reason": word}
                _finish(c, event_id, "failed", error=word, now=now, fields=parsed_fields,
                        envelope=parsed_envelope,
                        next_attempt_at=now + timedelta(seconds=backoff_seconds(attempts)))
        except Exception:  # noqa: BLE001 — the lease expires and another processor retries
            logger.warning("intake: could not record the failure of %s", event_id, exc_info=True)
        return {"id": event_id, "status": "failed", "reason": word}


def _fields_of(ev: IntakeEvent, conversation: Optional[str]) -> dict[str, Any]:
    return {"type": ev.type, "subject": ev.subject, "conversation_key": conversation,
            "actor_kind": ev.bossactor.kind, "actor_external_id": ev.bossactor.external_id or None,
            "screen": ev.bossscreen, "event_time": _event_time(ev.time)}


def _dead(connection: Connection, row: Mapping[str, Any], reason: str, *, now: datetime,
          fields: Optional[Mapping[str, Any]] = None, envelope: Any = None) -> None:
    _finish(connection, str(row["id"]), "dead", error=reason, now=now, fields=fields,
            envelope=envelope)
    if row.get("project_id"):
        emit_event(connection, str(row["project_id"]), "intake.event_dead", "intake",
                   "intake_event", str(row["id"]),
                   {"connection_id": row["connection_id"], "reason": reason[:300],
                    "attempts": int(row.get("attempts") or 0)})


def process_due(engine: Any, settings: Any = None, *, limit: int = PROCESSOR_BATCH,
                holder: str = HOLDER, now: Optional[datetime] = None) -> dict:
    """One pass: claim what is due, process each row in its own transaction."""
    from ..db import begin

    settings = settings or _settings()
    now = now or _now()
    with begin(engine) as c:
        rows = claim(c, limit=limit, holder=holder, now=now)
    out = {"claimed": len(rows), "routed": 0, "ignored": 0, "filtered": 0, "refused": 0,
           "failed": 0, "dead": 0, "received": 0, "deferred": 0, "digest": 0}
    for row in rows:
        got = process_one(engine, settings, row, now=now)
        out[got["status"]] = out.get(got["status"], 0) + 1
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Replay, listing, dead letters, retention
# ═══════════════════════════════════════════════════════════════════════════════════════════════

REPLAY_MODES = ("reprocess", "force")


def replay(connection: Connection, *, project_id: str, ids: list[str], actor: str,
           mode: str = "reprocess", reason: str = "") -> dict:
    """Put events back in the inbox. `reprocess` fires only routes that have not admitted a run
    for the event (the fires table's dedupe key `intake:<id>` makes an admitted route a no-op);
    `force` needs a typed reason and uses `intake:<id>:r<n>`, so every route runs once more.
    Identity and guards are re-evaluated as of now, by the processor."""
    if mode not in REPLAY_MODES:
        raise IntakeServiceError(f"mode must be one of {', '.join(REPLAY_MODES)}.")
    if mode == "force" and not str(reason or "").strip():
        raise IntakeServiceError("a forced replay needs a reason (it is logged with your name).",
                                 code="reason_required")
    wanted = [str(i) for i in ids if str(i).strip()][:500]
    if not wanted:
        raise IntakeServiceError("say which events to replay.")
    rows = connection.execute(text("""
        SELECT id, status, replays, raw IS NOT NULL AS has_raw FROM intake_events
         WHERE id = ANY(:ids) AND project_id = :p FOR UPDATE SKIP LOCKED"""),
        {"ids": wanted, "p": project_id}).mappings().all()
    replayed: list[str] = []
    skipped: list[dict] = []
    for r in rows:
        if r["status"] in ("received", "processing"):
            skipped.append({"id": r["id"], "reason": f"already {r['status']}"})
            continue
        if not r["has_raw"]:
            skipped.append({"id": r["id"], "reason": "raw body expired (retention)"})
            continue
        n = int(r["replays"] or 0) + 1
        marker = {"replay": n, "mode": mode, "by": actor, "at": _now().isoformat(),
                  "reason": reason[:300] or None, "from_status": r["status"]}
        connection.execute(text("""
            UPDATE intake_events
               SET status = 'received', attempts = 0, next_attempt_at = now(), lease_until = NULL,
                   last_error = NULL, replays = :n, replay_of = COALESCE(replay_of, id),
                   outcome = outcome || CAST(:m AS JSONB)
             WHERE id = :id"""), {"n": n, "m": json.dumps([marker]), "id": r["id"]})
        replayed.append(str(r["id"]))
    found = {str(r["id"]) for r in rows}
    skipped.extend({"id": i, "reason": "not found in this project"} for i in wanted
                   if i not in found)
    if replayed:
        emit_event(connection, project_id, "intake.events_replayed", actor, "intake_connection",
                   project_id, {"mode": mode, "count": len(replayed), "ids": replayed[:50],
                                "reason": reason[:300] or None})
    return {"mode": mode, "replayed": replayed, "skipped": skipped}


_EVENT_COLUMNS = ("id, connection_id, project_id, dedupe_key, source_event_id, type, subject, "
                  "conversation_key, status, headers, envelope, actor_kind, actor_external_id, "
                  "principal_id, screen, attempts, next_attempt_at, lease_until, claimed_by, "
                  "last_error, retry_count_seen, duplicates, outcome, replays, replay_of, trace_id, "
                  "verify_scheme, received_at, event_time, processed_at, raw_sha256, "
                  "octet_length(raw) AS raw_bytes")


def _summary(row: Mapping[str, Any], connections: Mapping[str, Mapping[str, Any]]) -> dict:
    env = _json(row.get("envelope"), {}) or {}
    data = env.get("data") or {}
    actor = env.get("bossactor") or {}
    outcome = _json(row.get("outcome"), []) or []
    runs = [o.get("run_id") for o in outcome if isinstance(o, dict) and o.get("run_id")]
    text_ = next((str(data[k]) for k in ("text", "title", "message", "comment") if data.get(k)), "")
    crow = connections.get(str(row["connection_id"]), {})
    return {
        "id": row["id"], "connection_id": row["connection_id"],
        "connection_name": crow.get("name") or "", "kind": crow.get("kind"),
        "status": row["status"], "type": row.get("type"), "subject": row.get("subject"),
        "text": text_[:200], "actor": {"kind": row.get("actor_kind") or actor.get("kind"),
                                      "display": actor.get("display") or "",
                                      "external_id": row.get("actor_external_id") or ""},
        "screen_verdict": ((_json(row.get("screen"), {}) or {}).get("verdict")),
        "attempts": int(row.get("attempts") or 0), "duplicates": int(row.get("duplicates") or 0),
        "retry_count_seen": int(row.get("retry_count_seen") or 0),
        "runs": runs, "last_error": row.get("last_error"),
        "received_at": _iso(row.get("received_at")), "event_time": _iso(row.get("event_time")),
        "processed_at": _iso(row.get("processed_at")),
        "next_attempt_at": _iso(row.get("next_attempt_at")) if row["status"] == "failed" else None,
        "trace_id": row.get("trace_id"), "replays": int(row.get("replays") or 0),
    }


def _connections_of(connection: Connection, project_id: str) -> dict[str, dict]:
    return {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT id, kind, name FROM intake_connections WHERE project_id = :p"),
        {"p": project_id}).mappings().all()}


def list_events(connection: Connection, project_id: str, *, status: Optional[str] = None,
                connection_id: Optional[str] = None, q: Optional[str] = None,
                since_hours: Optional[int] = None, limit: int = 100,
                before: Optional[str] = None) -> dict:
    """The stream (E5): newest first, with status chips; `q` searches type, subject, actor,
    the envelope's text and the dedupe key."""
    where = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "n": max(1, min(int(limit), 500))}
    if status:
        statuses = [s for s in str(status).split(",") if s in EVENT_STATUSES]
        if statuses:
            where.append("status = ANY(:statuses)")
            params["statuses"] = statuses
    if connection_id:
        where.append("connection_id = :c")
        params["c"] = connection_id
    if since_hours:
        where.append("received_at > now() - make_interval(hours => :h)")
        params["h"] = int(since_hours)
    if before:
        where.append("received_at < :before")
        params["before"] = _event_time(before)
    if q:
        where.append("(type ILIKE :q OR subject ILIKE :q OR dedupe_key ILIKE :q OR "
                     "actor_external_id ILIKE :q OR envelope::text ILIKE :q)")
        params["q"] = f"%{str(q)[:200]}%"
    rows = connection.execute(text(
        f"SELECT {_EVENT_COLUMNS} FROM intake_events WHERE {' AND '.join(where)} "
        "ORDER BY received_at DESC, id DESC LIMIT :n"), params).mappings().all()
    conns = _connections_of(connection, project_id)
    return {"events": [_summary(dict(r), conns) for r in rows],
            "counts": status_counts(connection, project_id, since_hours=since_hours or 24)}


def status_counts(connection: Connection, project_id: str, *, since_hours: int = 24) -> dict:
    rows = connection.execute(text(
        "SELECT status, count(*) AS n FROM intake_events WHERE project_id = :p "
        "AND received_at > now() - make_interval(hours => :h) GROUP BY status"),
        {"p": project_id, "h": int(since_hours)}).mappings().all()
    out = {s: 0 for s in EVENT_STATUSES}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    out["total"] = sum(out[s] for s in EVENT_STATUSES)
    return out


def get_event(connection: Connection, event_id: str, *, project_id: Optional[str] = None,
              include_raw: bool = True) -> Optional[dict]:
    """One event in full (E5's expanded row): envelope, guards, outcome, the raw body (decoded
    JSON when it is JSON and small) and the fires it caused."""
    row = connection.execute(text(
        f"SELECT {_EVENT_COLUMNS}, raw FROM intake_events WHERE id = :id"
        + (" AND project_id = :p" if project_id else "")),
        {"id": event_id, "p": project_id}).mappings().first()
    if row is None:
        return None
    row = dict(row)
    conns = _connections_of(connection, str(row.get("project_id") or project_id or ""))
    out = _summary(row, conns)
    out.update({
        "dedupe_key": row["dedupe_key"], "source_event_id": row.get("source_event_id"),
        "conversation_key": row.get("conversation_key"), "envelope": _json(row.get("envelope"), None),
        "headers": _json(row.get("headers"), {}), "screen": _json(row.get("screen"), None),
        "outcome": _json(row.get("outcome"), []), "verify_scheme": row.get("verify_scheme"),
        "raw_sha256": row.get("raw_sha256"), "raw_bytes": row.get("raw_bytes"),
        "replay_of": row.get("replay_of"), "principal_id": row.get("principal_id"),
        "claimed_by": row.get("claimed_by"),
    })
    raw = row.get("raw")
    if include_raw and raw is not None and len(raw) <= 64 * 1024:
        try:
            out["raw"] = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            out["raw_base64"] = base64.b64encode(bytes(raw)).decode("ascii")
    out["fires"] = [{**dict(r), "fired_at": _iso(r["fired_at"])} for r in connection.execute(text("""
        SELECT f.id, f.trigger_id, f.outcome, f.reason, f.run_id, f.dedupe_key, f.fired_at,
               t.name AS trigger_name, t.deployment_id
          FROM agent_trigger_fires f LEFT JOIN agent_triggers t ON t.id = f.trigger_id
         WHERE f.dedupe_key = :k OR f.dedupe_key LIKE :kr OR f.intake_event_id = :id
         ORDER BY f.fired_at"""),
        {"k": f"intake:{event_id}", "kr": f"intake:{event_id}:r%", "id": event_id}).mappings().all()]
    return out


def dead_letters(connection: Connection, project_id: str, *, limit: int = 100,
                 connection_id: Optional[str] = None) -> dict:
    """E6: what kept failing, why, how many tries, when it first failed; grouped counts per
    connection so the screen can say "Teams: nothing stuck"."""
    params: dict[str, Any] = {"p": project_id, "n": max(1, min(int(limit), 500))}
    extra = ""
    if connection_id:
        extra = " AND connection_id = :c"
        params["c"] = connection_id
    rows = connection.execute(text(
        f"SELECT {_EVENT_COLUMNS} FROM intake_events WHERE project_id = :p AND status = 'dead'"
        f"{extra} ORDER BY processed_at DESC NULLS LAST, received_at DESC LIMIT :n"),
        params).mappings().all()
    conns = _connections_of(connection, project_id)
    per = {cid: {"connection_id": cid, "kind": c.get("kind"), "name": c.get("name") or "", "dead": 0}
           for cid, c in conns.items()}
    for r in connection.execute(text(
            "SELECT connection_id, count(*) AS n FROM intake_events WHERE project_id = :p "
            "AND status = 'dead' GROUP BY connection_id"), {"p": project_id}).mappings().all():
        if str(r["connection_id"]) in per:
            per[str(r["connection_id"])]["dead"] = int(r["n"])
    items = []
    for r in rows:
        item = _summary(dict(r), conns)
        outcome = _json(r.get("outcome"), []) or []
        item["routes"] = [o.get("trigger_id") for o in outcome
                          if isinstance(o, dict) and o.get("trigger_id")]
        item["first_failed_at"] = _iso(r.get("received_at"))
        item["dead_at"] = _iso(r.get("processed_at"))
        items.append(item)
    return {"dead_letters": items, "per_connection": sorted(per.values(), key=lambda x: x["name"])}


def sweep_retention(connection: Connection, *, now: Optional[datetime] = None) -> dict:
    """§4.5: raw bodies are nulled after 14 days (the hash stays); rows go after 30 days
    (routed/ignored/...) or 90 days (dead/refused); a row still being worked is never touched."""
    now = now or _now()
    nulled = connection.execute(text(
        "UPDATE intake_events SET raw = NULL WHERE raw IS NOT NULL AND raw_expires_at < :now "
        "AND status NOT IN ('received', 'processing', 'failed')"), {"now": now}).rowcount
    deleted = connection.execute(text(
        "DELETE FROM intake_events WHERE expires_at < :now "
        "AND status NOT IN ('received', 'processing', 'failed')"), {"now": now}).rowcount
    return {"raw_nulled": int(nulled or 0), "deleted": int(deleted or 0)}


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The background processor (every API process; competing consumers)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

_STARTED = threading.Event()
_STOP = threading.Event()


class _AnyInterest:
    def matches(self, note: dict) -> bool:
        return True


def start(engine: Any, settings: Any) -> bool:
    """The processor thread (system_threads.start_all): woken by `boss_intake` NOTIFY, with a
    2 s safety tick; retention sweeps once a minute; beats system_services."""
    if _STARTED.is_set():
        return False
    _STARTED.set()
    _STOP.clear()
    wake = threading.Event()

    def listen() -> None:
        try:
            from .event_stream import Hub, PgListener, libpq_dsn

            hub = Hub()
            hub.listen(_AnyInterest(), lambda _note: wake.set())
            PgListener(libpq_dsn(engine), hub, channel="boss_intake", poll=1.0).start()
        except Exception:  # noqa: BLE001 — the 2 s tick carries the processor without NOTIFY
            logger.warning("intake: NOTIFY listener not started; ticking only", exc_info=True)

    def loop() -> None:
        from ..db import begin
        from . import system_services

        listen()
        system_services.register(engine, SERVICE_KEY, started=True)
        last_sweep = time.monotonic()
        tick = float(getattr(settings, "intake_processor_tick_seconds", PROCESSOR_TICK_SECONDS)
                     or PROCESSOR_TICK_SECONDS)
        while not _STOP.is_set():
            wake.wait(tick)
            wake.clear()
            try:
                got = process_due(engine, settings)
                while got["claimed"] >= PROCESSOR_BATCH and not _STOP.is_set():
                    got = process_due(engine, settings)
                flush_refusals(engine)
                if time.monotonic() - last_sweep > 60:
                    last_sweep = time.monotonic()
                    with begin(engine) as c:
                        sweep_retention(c)
                system_services.beat(engine, SERVICE_KEY, ok=True)
            except Exception as error:  # noqa: BLE001 — the next tick tries again
                logger.warning("intake processor tick failed", exc_info=True)
                try:
                    system_services.beat(engine, SERVICE_KEY, ok=False,
                                         error=f"{type(error).__name__}: {error}"[:300])
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=loop, daemon=True, name="boss-intake-processor").start()
    return True


def stop() -> None:
    _STOP.set()
    _STARTED.clear()


__all__ = [
    "BACKOFF_SECONDS", "BUCKETS", "CONNECTION_STATUSES", "DEFAULT_LIMITS", "LEASE_SECONDS",
    "MAX_ATTEMPTS", "MAX_HOPS", "PATH_PREFIX", "REFUSALS", "REPLAY_MODES", "ROTATION_GRACE_SECONDS",
    "SERVICE_KEY", "SHED_RAW_BYTES", "flush_refusals",
    "IntakeServiceError", "backoff_seconds", "claim", "connection_by_token", "create_connection",
    "dead_letters", "delete_connection", "describe_connection", "get_connection", "get_event",
    "inbound_path", "list_connections", "list_events", "process_due", "process_one", "receive",
    "replay", "rotate_secret", "set_connection_status", "start", "status_counts", "stop",
    "sweep_retention", "test_connection", "to_intake_connection", "update_connection",
]
