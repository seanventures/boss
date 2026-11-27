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
import hashlib
import json
import logging
import re
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
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    RawRequest,
    RawResponse,
    OutboxOp,
    StoredRaw,
    VerifyRefused,
    kept_headers,
    lower_headers,
)
from . import intake_router
from .core import emit_event, make_id
from .redaction import mask_url_credentials, mask_url_credentials_in

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
    _refuse_attestation(config)
    for key, value in dict(config or {}).items():
        if key in secret_fields:
            given_secrets.setdefault(key, str(value))      # a secret typed into config is a secret
            continue
        cfg[str(key)] = value
    for f in manifest.auth_fields:
        if not f.get("secret") and _required_in(f, mode) and cfg.get(f["name"]) in (None, ""):
            raise IntakeServiceError(f"{f['label']} is required.", code="missing_field")
    # ONE CHAT APP PER INSTALL (§15 D1): a chat source is bound to the install's chat endpoint
    # and uses ITS credentials — nothing is minted or typed twice.
    endpoint = _bind_chat_endpoint(connection, project_id, adapter.key, cfg)
    endpoint_secrets = set((_json((endpoint or {}).get("secret_refs"), {}) or {}))
    connection_id = make_id("icn")
    shown: dict[str, str] = {}
    refs: dict[str, str] = {}
    for f in manifest.auth_fields:
        if not f.get("secret"):
            continue
        fname = f["name"]
        value = given_secrets.get(fname)
        if not value and fname in endpoint_secrets:
            continue
        if not value and fname == "signing_secret":
            value = _mint(fname)
            shown[fname] = value
        if not value:
            if _required_in(f, mode):
                raise IntakeServiceError(f"{f['label']} is required.", code="missing_secret")
            continue
        refs[fname] = _store_secret(connection, project_id, connection_id, fname, value, actor)
    path_token = _secrets.token_urlsafe(24)
    link = _standing_link(connection)
    connection.execute(text("""
        INSERT INTO intake_connections (id, project_id, org_id, kind, name, mode, config,
               secret_refs, path_token, status, limits, created_by, on_behalf_of, grant_id,
               chat_endpoint_id)
        VALUES (:id, :p, :org, :k, :n, :m, CAST(:c AS JSONB), CAST(:s AS JSONB), :tok, 'active',
                CAST(:l AS JSONB), :by, :obo, :grant, :che)"""),
        {"id": connection_id, "p": project_id, "org": org_id, "k": adapter.key, "n": name[:120],
         "che": (endpoint or {}).get("id"),
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


def _required_in(field: Mapping[str, Any], mode: str) -> bool:
    """A manifest auth field is required in `mode`: a field that names its `modes` is required
    exactly in those (GitHub's webhook secret in push, its repository in poll); any other field
    by its `required` flag (default true)."""
    if isinstance(field.get("modes"), list):
        return mode in field["modes"]
    return bool(field.get("required", True))


#: Chat sources whose connection rides on a chat endpoint (§15 D1), by intake kind.
CHAT_ENDPOINT_KINDS = {"slack": "slack", "teams": "teams"}


def _bind_chat_endpoint(connection: Connection, project_id: str, kind: str,
                        cfg: dict) -> Optional[dict]:
    """The chat endpoint a chat connection rides on (`config.chat_endpoint_id`): same project,
    same channel kind, active. Its team/tenant/app ids are copied into the connection's config when
    the connection names none (the guest / other-workspace / JWT checks read them)."""
    wanted = CHAT_ENDPOINT_KINDS.get(kind)
    endpoint_id = str(cfg.get("chat_endpoint_id") or "").strip()
    if not wanted:
        if endpoint_id:
            raise IntakeServiceError(f"a {kind} connection does not use a chat endpoint.")
        return None
    row = connection.execute(text(
        "SELECT * FROM chat_endpoints WHERE id = :e AND project_id = :p"),
        {"e": endpoint_id, "p": project_id}).mappings().first() if endpoint_id else None
    if row is None:
        raise IntakeServiceError(f"chat_endpoint_id must name a {wanted} chat endpoint of this "
                                 "project (Settings › Chat).", code="missing_field")
    if row["kind"] != wanted or row["status"] != "active":
        raise IntakeServiceError(f"chat endpoint {endpoint_id} is not an active {wanted} "
                                 "endpoint.", code="bad_endpoint")
    taken = connection.execute(text(
        "SELECT id FROM intake_connections WHERE chat_endpoint_id = :e LIMIT 1"),
        {"e": endpoint_id}).scalar()
    if taken:
        raise IntakeServiceError(f"chat endpoint {endpoint_id} already has an intake connection "
                                 f"({taken}): one chat app, one connection.", status=409,
                                 code="endpoint_taken")
    ecfg = _json(row.get("config"), {}) or {}
    for key in ("team_id", "bot_user_id", "bot_id", "app_id", "tenant_id",
                "service_url", "oidc_config_url", "token_url"):
        if ecfg.get(key) and not cfg.get(key):
            cfg[key] = ecfg[key]
    cfg["chat_endpoint_id"] = endpoint_id
    return dict(row)


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
        "config": mask_url_credentials_in(_json(row.get("config"), {}) or {}),
        "secret_fields": sorted(k for k in refs if not k.endswith("_previous")),   # names only
        "url_path": inbound_path(str(row["kind"]), str(row["path_token"])),
        "chat_endpoint_id": row.get("chat_endpoint_id"),
        "limits": _limits(row.get("limits")), "health": mask_url_credentials_in(health),
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


def _keep_shown_secret(stored: Any, incoming: Any) -> Any:
    """A read view sends a masked URL back unchanged. That is the stored secret, not a new one."""
    if isinstance(stored, str) and isinstance(incoming, str):
        shown = mask_url_credentials(stored)
        if incoming == shown and shown != stored:
            return stored
        return incoming
    if isinstance(stored, dict) and isinstance(incoming, dict):
        return {key: _keep_shown_secret(stored[key], value) if key in stored else value
                for key, value in incoming.items()}
    if isinstance(stored, list) and isinstance(incoming, list) and len(stored) == len(incoming):
        return [_keep_shown_secret(old, new) for old, new in zip(stored, incoming)]
    return incoming


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
    _refuse_attestation(config)
    if config is not None and "chat_endpoint_id" in dict(config) and \
            str(dict(config)["chat_endpoint_id"] or "") != str(row.get("chat_endpoint_id") or ""):
        raise IntakeServiceError("a connection's chat endpoint is fixed: create a new connection "
                                 "for another Slack app.", code="endpoint_fixed")
    if config is not None:
        for key, value in dict(config).items():
            if key in secret_fields:
                given_secrets.setdefault(key, str(value))
            elif value is None:
                cfg.pop(str(key), None)
            else:
                cfg[str(key)] = _keep_shown_secret(cfg.get(str(key)), value)
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


#: The config key the SSO attestation lives under: written ONLY by `set_sso_attestation` (an
#: identity-provider admin, server-stamped), never by a connection create/update.
ATTESTATION_KEY = "sso_attested"


def _refuse_attestation(config: Optional[Mapping[str, Any]]) -> None:
    if config and ATTESTATION_KEY in dict(config):
        raise IntakeServiceError(
            "SSO attestation is set by an identity-provider admin with "
            "PUT /v1/intake/connections/{id}/sso-attestation, not in the connection's config.",
            code="attestation_not_in_config")


def _org_of(connection: Connection, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()


def set_sso_attestation(connection: Connection, *, project_id: str, connection_id: str,
                        actor: str, identity_provider_id: str,
                        domains: Optional[list] = None) -> dict:
    """An org admin states that this source's workspace signs people in through BOSS's IdP
    `identity_provider_id` (§6.3): chat users whose email is in `domains` may be auto-linked.
    The provider must be an ACTIVE provider of the connection's org, and every domain one of
    ITS verified domains (default: all of them). Who attested and when are stamped here."""
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    org = _org_of(connection, project_id)
    idp = connection.execute(text(
        "SELECT id, name, domains, status FROM identity_providers WHERE id = :i AND org_id = :o"),
        {"i": identity_provider_id, "o": org}).mappings().first()
    if idp is None or idp["status"] != "active":
        raise IntakeServiceError("no active identity provider with that id in this org.",
                                 status=404, code="unknown_idp")
    verified = sorted({str(d).lower().lstrip("@") for d in (idp["domains"] or []) if str(d).strip()})
    if not verified:
        raise IntakeServiceError("that identity provider has no verified domains.",
                                 code="idp_has_no_domains")
    wanted = sorted({str(d).lower().lstrip("@").strip() for d in (domains or verified)
                     if str(d).strip()})
    outside = [d for d in wanted if d not in verified]
    if outside or not wanted:
        raise IntakeServiceError(f"not a verified domain of {idp['name']}: "
                                 f"{', '.join(outside) or '(none given)'}.", code="domain_not_verified")
    cfg = dict(_json(row.get("config"), {}) or {})
    cfg[ATTESTATION_KEY] = {"idp": idp["id"], "idp_name": idp["name"], "domains": wanted,
                            "attested_by": actor, "attested_at": _now().isoformat()}
    connection.execute(text("UPDATE intake_connections SET config = CAST(:c AS JSONB), "
                            "updated_at = now() WHERE id = :id"),
                       {"c": json.dumps(cfg, default=str), "id": connection_id})
    emit_event(connection, project_id, "intake.connection_sso_attested", actor,
               "intake_connection", connection_id, {"idp": idp["id"], "domains": wanted})
    return describe_connection(connection, get_connection(connection, connection_id) or {})


def clear_sso_attestation(connection: Connection, *, project_id: str, connection_id: str,
                          actor: str) -> dict:
    row = get_connection(connection, connection_id, project_id=project_id, lock=True)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    cfg = dict(_json(row.get("config"), {}) or {})
    had = cfg.pop(ATTESTATION_KEY, None)
    connection.execute(text("UPDATE intake_connections SET config = CAST(:c AS JSONB), "
                            "updated_at = now() WHERE id = :id"),
                       {"c": json.dumps(cfg, default=str), "id": connection_id})
    if had:
        emit_event(connection, project_id, "intake.connection_sso_attestation_removed", actor,
                   "intake_connection", connection_id, {})
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


_SAFE_RECEIPT = ("channel", "ts")


def _test_channel(connection: Connection, row: Mapping[str, Any]) -> str:
    """The channel the test post goes to: the connection's, else the chat endpoint's."""
    cfg = _json(row.get("config"), {}) or {}
    channel = str(cfg.get("channel") or "").strip().lstrip("#")
    if channel:
        return channel
    endpoint_id = row.get("chat_endpoint_id") or cfg.get("chat_endpoint_id")
    if not endpoint_id:
        return ""
    raw = connection.execute(text("SELECT config FROM chat_endpoints WHERE id = :e"),
                             {"e": endpoint_id}).scalar()
    return str((_json(raw, {}) or {}).get("channel") or "").strip().lstrip("#")


def _endpoint_secret_names(connection: Connection, row: Mapping[str, Any]) -> set[str]:
    cfg = _json(row.get("config"), {}) or {}
    endpoint_id = row.get("chat_endpoint_id") or cfg.get("chat_endpoint_id")
    if not endpoint_id:
        return set()
    refs = _json(connection.execute(text(
        "SELECT secret_refs FROM chat_endpoints WHERE id = :e"),
        {"e": endpoint_id}).scalar(), {}) or {}
    return {str(name) for name in refs}


def _with_endpoint_secrets(connection: Connection, settings: Any, row: Mapping[str, Any],
                           checked: dict) -> dict:
    """`test_connection` checks the connection's own refs. A Slack bot token lives on the chat
    endpoint, and that is the secret the test post sends with, so it is checked here too."""
    conn = to_intake_connection(connection, settings, row)
    checks = dict(checked["checks"])
    for fname in sorted(_endpoint_secret_names(connection, row)):
        key = f"secret:{fname}"
        if key in checks:
            continue
        try:
            checks[key] = "ok" if conn.secret(fname) else "empty"
        except Exception as error:  # noqa: BLE001 — the reason, never the value
            checks[key] = f"unresolvable ({type(error).__name__})"
    adapter_ok = checks["adapter"]["ok"] if isinstance(checks.get("adapter"), dict) else True
    secret_ok = all(v == "ok" for k, v in checks.items() if k.startswith("secret:"))
    checked = dict(checked)
    checked["checks"] = checks
    checked["ok"] = bool(secret_ok) and bool(adapter_ok)
    health_row = get_connection(connection, str(row["id"])) or {}
    health = dict(_json(health_row.get("health"), {}) or {})
    if health.get("last_test_ok") is not checked["ok"]:
        health["last_test_ok"] = checked["ok"]
        connection.execute(text(
            "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = now() "
            "WHERE id = :id"),
            {"h": json.dumps(health, default=str), "id": row["id"]})
    return checked


def _safe_receipt(ref: Mapping[str, Any]) -> dict:
    return {k: ref[k] for k in _SAFE_RECEIPT if ref.get(k) not in (None, "")}


def _store_probe(connection: Connection, connection_id: str, probe: dict) -> None:
    row = get_connection(connection, connection_id) or {}
    health = dict(_json(row.get("health"), {}) or {})
    health["test_probe"] = probe
    connection.execute(text(
        "UPDATE intake_connections SET health = CAST(:h AS JSONB), updated_at = now() "
        "WHERE id = :id"),
        {"h": json.dumps(health, default=str), "id": connection_id})


def _raw_event(raw: Any) -> dict:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, str)):
        try:
            body = json.loads(raw)
        except ValueError:
            return {}
    elif isinstance(raw, dict):
        body = raw
    else:
        return {}
    if not isinstance(body, dict):
        return {}
    event = body.get("event")
    return event if isinstance(event, dict) else {}


def _is_person_address(ev: Mapping[str, Any], channel: str, bot_user: str) -> bool:
    """A person speaking to BOSS after the probe: an app mention, a DM, or a message that
    names the bot, on the probe's channel. The bot's own echo is not that reply."""
    if str(ev.get("actor_kind") or "") == "bot":
        return False
    env = _json(ev.get("envelope"), {}) or {}
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    conv = env.get("bossconversation") if isinstance(env.get("bossconversation"), dict) else {}
    raw_ev = _raw_event(ev.get("raw"))
    user = str(data.get("user") or raw_ev.get("user") or "")
    if data.get("bot_id") or raw_ev.get("bot_id"):
        return False
    if bot_user and user == bot_user:
        return False
    if str(raw_ev.get("subtype") or "") in ("bot_message", "message_changed", "message_deleted"):
        return False
    if not user and str(ev.get("actor_kind") or "") != "user":
        return False
    ch = str(conv.get("channel") or data.get("channel") or raw_ev.get("channel") or "")
    channel_type = str(data.get("channel_type") or raw_ev.get("channel_type") or "")
    im = channel_type == "im"
    kind = str(ev.get("type") or env.get("type") or raw_ev.get("type") or "")
    text_ = str(data.get("text") or raw_ev.get("text") or "")
    mentioned = bool(bot_user) and f"<@{bot_user}>" in text_
    addressed = ("app_mention" in kind or "message.mention" in kind or im
                 or mentioned or data.get("mentions_bot") is True)
    if not addressed:
        return False
    if channel and ch and ch != channel and not im:
        return False
    return True


def _person_reply(connection: Connection, row: Mapping[str, Any], probe: Mapping[str, Any]
                  ) -> Optional[dict]:
    posted_at = probe.get("posted_at")
    if not posted_at:
        return None
    cfg = _json(row.get("config"), {}) or {}
    bot_user = str(cfg.get("bot_user_id") or "")
    rows = connection.execute(text("""
        SELECT id, status, raw, actor_kind, type, envelope
          FROM intake_events
         WHERE connection_id = :c
           AND received_at >= CAST(:at AS timestamptz)
           AND status <> 'shed'
         ORDER BY received_at, id
         LIMIT 100"""), {"c": row["id"], "at": posted_at}).mappings().all()
    for ev in rows:
        if _is_person_address(ev, str(probe.get("channel") or ""), bot_user):
            return dict(ev)
    return None


def _probe_of(row: Mapping[str, Any]) -> dict:
    health = _json(row.get("health"), {}) or {}
    probe = health.get("test_probe")
    return dict(probe) if isinstance(probe, dict) else {}


def post_test_message(connection: Connection, settings: Any, *, project_id: str,
                      connection_id: str) -> dict:
    """Post BOSS's own test message into the connection's channel and say whether a person has
    already answered it. The post goes through the intake adapter (`send` / `OutboxOp`); a source
    that cannot reply is refused and nothing is sent. Secrets are checked first: a secret that
    does not resolve does not post."""
    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    adapter = _adapter(str(row["kind"]))
    if not adapter.can("reply"):
        raise IntakeServiceError("This source cannot take a test post.", status=422,
                                 code="reply_unsupported")
    channel = _test_channel(connection, row)
    if not channel:
        raise IntakeServiceError("Name the channel BOSS posts the test in.", status=422,
                                 code="channel_required")
    checked = _with_endpoint_secrets(connection, settings, row,
                                    test_connection(connection, settings, project_id=project_id,
                                                    connection_id=connection_id))
    out = {"ok": checked["ok"], "checks": checked["checks"], "channel": channel,
           "posted": False, "round_trip": False, "probe_id": None, "receipt": {},
           "event_id": None, "event_status": None,
           "last_hour": checked.get("last_hour"), "backlog": checked.get("backlog"),
           "dead": checked.get("dead"), "last_event_at": checked.get("last_event_at")}
    if not checked["ok"]:
        return out
    probe_id = make_id("itp")
    words = f"BOSS test {probe_id}. Mention @BOSS in this channel to confirm the round trip."
    op = OutboxOp(op="post", conversation={"channel": channel}, message={"text": words},
                  dedupe_key=f"intake_test:{connection_id}:{probe_id}")
    conn = to_intake_connection(connection, settings, row)
    try:
        receipt = adapter.send(conn, op, conn.secrets)
    except Exception as error:  # noqa: BLE001 — the type only; the message may carry a token
        out["ok"] = False
        out["error"] = type(error).__name__
        return out
    if not getattr(receipt, "ok", False):
        out["ok"] = False
        out["error"] = "send_failed"
        return out
    posted_at = connection.execute(text("SELECT clock_timestamp()")).scalar()
    safe = _safe_receipt(dict(getattr(receipt, "ref", None) or {}))
    _store_probe(connection, connection_id, {
        "id": probe_id, "channel": channel, "posted_at": _iso(posted_at), "receipt": safe})
    out.update(posted=True, probe_id=probe_id, receipt=safe)
    return out


def confirm_test_message(connection: Connection, *, project_id: str, connection_id: str) -> dict:
    """Whether the latest test post has a person's reply. Does not post again."""
    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    probe = _probe_of(row)
    reply = _person_reply(connection, row, probe) if probe.get("id") else None
    return {"ok": bool(probe.get("id")), "posted": bool(probe.get("id")),
            "channel": probe.get("channel"), "probe_id": probe.get("id"),
            "receipt": dict(probe.get("receipt") or {}),
            "round_trip": reply is not None,
            "event_id": None if reply is None else reply["id"],
            "event_status": None if reply is None else reply["status"],
            "checks": {}}


def to_intake_connection(connection: Connection, settings: Any, row: Mapping[str, Any]
                         ) -> IntakeConnection:
    """The adapter's view of a row; secrets resolve from the backend when asked, never before."""
    refs = dict(_json(row.get("secret_refs"), {}) or {})
    project_id = row.get("project_id")
    config = dict(_json(row.get("config"), {}) or {})
    if row.get("chat_endpoint_id"):
        # §15 D1: the endpoint's secrets (signing secret, bot token) resolve through the
        # connection; a secret the connection stores itself overrides.
        endpoint_refs = _json(connection.execute(text(
            "SELECT secret_refs FROM chat_endpoints WHERE id = :e"),
            {"e": row["chat_endpoint_id"]}).scalar(), {}) or {}
        for name, ref in endpoint_refs.items():
            refs.setdefault(name, ref)
        config["chat_endpoint_id"] = row["chat_endpoint_id"]

    def resolve(name: str) -> Optional[str]:
        ref = refs.get(name)
        if not ref:
            return None
        from .secret_backends import resolve_secret_ref

        return resolve_secret_ref(connection, ref, settings, project_id=project_id)

    return IntakeConnection(
        str(row["id"]), str(project_id or ""), str(row["kind"]), mode=str(row["mode"]),
        config=config, secret_resolver=resolve,
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
    return accept(engine, adapter, row, request, verified, settings=settings, now=now_s)


def accept_in_tx(connection: Connection, adapter: Any, row: Mapping[str, Any],
                 request: RawRequest, verified: Any, *, settings: Any = None,
                 now: Optional[float] = None, handshake: bool = True) -> dict:
    """In-transaction step 2 of the front door: handshake / paused check / deduped insert into
    `intake_events`. Callers already holding a transaction (`ticket_sync.ingest_webhook`,
    `/v1/webhooks/github`) invoke this directly without opening a second transaction."""
    now_s = time.time() if now is None else now
    settings = settings or _settings()
    conn = to_intake_connection(connection, settings, row)
    if handshake and adapter.can("handshake"):
        shake = adapter.handshake(conn, request)
        if shake is not None:
            return {"outcome": ACK_HANDSHAKE, "http_status": shake.status,
                    "body": shake.body, "headers": dict(shake.headers),
                    "media_type": shake.media_type}
    if row["status"] != "active":
        key = "dropped_while_" + str(row["status"])
        connection.execute(text("""
            UPDATE intake_connections SET health = health || CAST(:h AS JSONB)
             WHERE id = :id"""),
            {"h": json.dumps({key: int((_json(row.get("health"), {}) or {}).get(key)
                                        or 0) + 1}), "id": row["id"]})
        return _ack(adapter, conn, request, ACK_PAUSED)
    dedupe_key = str(adapter.dedupe_key(conn, request))[:500]
    limits = _limits(row.get("limits"))
    over_rate, shed = BUCKETS.admit(str(row["id"]), limits, now_s)
    if over_rate and not shed:
        backlog = int(connection.execute(text(
            "SELECT count(*) FROM intake_events WHERE connection_id = :c "
            "AND status IN ('received', 'processing', 'failed')"),
            {"c": row["id"]}).scalar() or 0)
        shed = backlog >= int(limits["max_backlog"])
    retry = verified.retry or _retry_signal(adapter, request)
    event_id = make_id("iev")
    received = _now()
    claimed_dedupe = connection.execute(text("""
        INSERT INTO intake_event_dedupe (connection_id, dedupe_key, event_id, received_at)
        VALUES (:c, :dk, :id, :at)
        ON CONFLICT (connection_id, dedupe_key) DO NOTHING RETURNING event_id"""),
        {"c": row["id"], "dk": dedupe_key, "id": event_id, "at": received}).scalar()
    if claimed_dedupe is None:
        connection.execute(text("""
            UPDATE intake_events SET duplicates = duplicates + 1,
                   retry_count_seen = GREATEST(retry_count_seen, :retry)
             WHERE connection_id = :c AND dedupe_key = :dk"""),
            {"retry": int((retry or {}).get("num") or 0), "c": row["id"], "dk": dedupe_key})
        return _ack(adapter, conn, request, ACK_DUPLICATE, dedupe_key=dedupe_key)
    connection.execute(text("""
        INSERT INTO intake_events (id, connection_id, project_id, dedupe_key,
               source_event_id, status, raw, raw_sha256, headers, retry_count_seen,
               trace_id, verify_scheme, received_at, raw_expires_at, expires_at)
        VALUES (:id, :c, :p, :dk, :sid, :st, :raw, :sha, CAST(:h AS JSONB), :retry, :tr,
                :vs, :at, :rexp, :exp)"""),
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
         "exp": received + timedelta(days=ENVELOPE_RETENTION_DAYS)})
    if _BEFORE_COMMIT is not None:
        _BEFORE_COMMIT(event_id)
    outcome = ACK_SHED if shed else ACK_RECEIVED
    ack = _ack(adapter, conn, request, outcome, event_id=event_id)
    ack["event_id"] = event_id
    ack["shed"] = shed
    return ack


def accept(engine: Any, adapter: Any, row: Mapping[str, Any], request: RawRequest, verified: Any,
           *, settings: Any = None, now: Optional[float] = None, handshake: bool = True) -> dict:
    """Step 2 of the front door, for a delivery whose proof was already accepted (the push route
    after `verify`; a Socket Mode consumer, whose socket the app-level token authenticated): a
    handshake stores nothing; a paused connection stores nothing and still gets a 2xx;
    everything else is ONE insert, deduplicated on the source's own id, committed before the
    answer."""
    from ..db import begin

    try:
        with begin(engine) as c:
            return accept_in_tx(c, adapter, row, request, verified, settings=settings,
                                now=now, handshake=handshake)
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


def _due_now(connection: Connection, now: Optional[datetime]) -> datetime:
    """The clock `next_attempt_at` is stamped with. Inserts use `DEFAULT now()` (the database
    clock). A host clock that trails the database leaves a row inserted a moment ago not yet
    due, so a pass that does not name `now` reads the database clock."""
    if now is not None:
        return now
    stamped = connection.execute(text("SELECT clock_timestamp()")).scalar()
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return stamped


def claim(connection: Connection, *, limit: int = PROCESSOR_BATCH, holder: str = HOLDER,
          now: Optional[datetime] = None, project_id: Optional[str] = None) -> list[dict]:
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
    the thread in admit order, and the agent sees every earlier turn in the thread's history.
    `project_id` limits the claim to one project (tests on a shared database)."""
    from . import projects_archive

    now = _due_now(connection, now)
    rows = connection.execute(text(f"""
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY connection_id ORDER BY received_at, id) AS rn,
                   received_at
              FROM intake_events
             WHERE ((status IN ('received', 'failed') AND next_attempt_at <= :now
                     AND (lease_until IS NULL OR lease_until < :now))
                    OR (status = 'processing' AND lease_until < :now))
               AND (CAST(:p AS TEXT) IS NULL OR project_id = CAST(:p AS TEXT))
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
         "n": int(limit), "p": project_id}).mappings().all()
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
            crow_config = _json(crow.get("config"), {}) or {}
            ticket_provider_id = str(crow_config.get("work_tracker_provider_id") or "").strip()
            ticket_outcome: Optional[dict[str, Any]] = None
            if ticket_provider_id:
                from . import ticket_sync

                ticket_outcome = ticket_sync.process_intake_event(
                    c, settings, provider_id=ticket_provider_id, raw_body=stored.body
                )
                if ticket_outcome.get("status") == "dead":
                    reason = str(ticket_outcome.get("reason") or "ticket connection deleted")
                    _finish(c, event_id, "dead", error=reason, now=now)
                    return {"id": event_id, "status": "dead", "reason": reason}
                all_outcomes.append({"ticket_pickup": ticket_outcome})
            primary: Optional[IntakeEvent] = None
            primary_conversation: Optional[str] = None
            filtered_reason = ""
            lifecycle_actions: list[tuple[str, str, str]] = []
            for ev in events:
                ev.bossconnection = conn.id
                ev.bossdedupe = ev.bossdedupe or str(row["dedupe_key"])
                if ev.type.startswith("com.microsoft.outlook.subscription.") and ev.data.get("lifecycle_event"):
                    lifecycle_actions.append((
                        conn.id,
                        str(ev.data.get("subscription_id") or ""),
                        str(ev.data.get("lifecycle_event") or ""),
                    ))
                    all_outcomes.append({"lifecycle": ev.data.get("lifecycle_event"),
                                         "subscription_id": ev.data.get("subscription_id")})
                    if primary is None:
                        primary = ev
                        parsed_fields = _fields_of(primary, None)
                        parsed_envelope = primary.to_dict()
                    continue
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
                if crow.get("chat_endpoint_id") and isinstance(ev.bossconversation, dict) and \
                        isinstance(ev.bossconversation.get("conversation_ref"), dict) and \
                        ev.bossconversation["conversation_ref"]:
                    # Refresh conversation_ref (serviceUrl can change) & last_inbound_at (§9.3).
                    conv_ref = ev.bossconversation["conversation_ref"]
                    ch_ext = str(ev.bossconversation.get("channel") or ev.data.get("channel") or "")
                    th_ext = str(ev.bossconversation.get("thread_ts") or conversation or ch_ext)
                    if ch_ext and th_ext:
                        c.execute(text("""
                            INSERT INTO chat_threads (id, endpoint_id, project_id, external_channel_id,
                                   external_thread_id, conversation_key, conversation_ref,
                                   last_inbound_at)
                            VALUES (:i, :e, :p, :ch, :th, :ck, CAST(:cref AS JSONB), :now)
                            ON CONFLICT (endpoint_id, external_channel_id, external_thread_id)
                            DO UPDATE SET
                                conversation_ref = EXCLUDED.conversation_ref,
                                conversation_key = COALESCE(EXCLUDED.conversation_key,
                                                            chat_threads.conversation_key),
                                last_inbound_at = EXCLUDED.last_inbound_at,
                                updated_at = now()"""),
                            {"i": make_id("cht"), "e": crow["chat_endpoint_id"],
                             "p": conn.project_id, "ch": ch_ext[:200], "th": th_ext[:200],
                             "ck": conversation, "cref": json.dumps(conv_ref, default=str),
                             "now": now})
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
                # THE SAME COMMIT BY WEBHOOK AND BY POLL is one event (BOSSAPP-1175). The source
                # names the commit (`data.head_key`); a copy that arrived the OTHER way and was
                # already processed wins. Same-way copies are the inbox's dedupe key's job, and
                # a forced replay always runs.
                head_key = str(ev.data.get("head_key") or "")
                if head_key and conversation and not force_n:
                    by = str(ev.data.get("delivered_by") or "webhook")
                    # Across the project's connections of this kind too: a webhook connection
                    # and a poll connection on one repository see the same commit once. The
                    # lock is per repository and commit, so two connections cannot both pass.
                    c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                              {"k": f"{conn.project_id}:{conn.kind}:{head_key}"})
                    twin = c.execute(text("""
                        SELECT e.id FROM intake_events e
                          JOIN intake_connections ic ON ic.id = e.connection_id
                         WHERE ic.project_id = :p AND ic.kind = :kind AND e.id <> :id
                           AND e.status IN ('routed', 'filtered', 'ignored', 'digest', 'refused')
                           AND e.envelope -> 'data' ->> 'head_key' = :k
                           AND (e.connection_id <> :c OR
                                COALESCE(e.envelope -> 'data' ->> 'delivered_by', 'webhook')
                                <> :by)
                         LIMIT 1"""),
                        {"p": conn.project_id, "kind": conn.kind, "c": conn.id,
                         "id": event_id, "k": head_key, "by": by}).scalar()
                    if twin:
                        filtered_reason = f"the same commit already arrived as {twin}"
                        all_outcomes.append({"filtered": filtered_reason, "event": ev.id,
                                             "duplicate_of": twin})
                        continue
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
            elif ticket_outcome is not None:
                if ticket_outcome.get("status") in ("waiting", "started", "imported"):
                    status = "routed"
                else:
                    status = "ignored"
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
            result_out = {"id": event_id, "status": status, "outcome": all_outcomes}
        if lifecycle_actions:
            from . import intake_subscriptions

            for cid, sub_id, lc_ev in lifecycle_actions:
                try:
                    intake_subscriptions.handle_lifecycle(
                        engine, settings, connection_id=cid,
                        provider_subscription_id=sub_id, lifecycle_event=lc_ev, now=now,
                    )
                except Exception:  # noqa: BLE001 — renewer retries on next tick
                    logger.warning("lifecycle handler failed for %s (%s)", cid, lc_ev, exc_info=True)
        return result_out
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
                holder: str = HOLDER, now: Optional[datetime] = None,
                project_id: Optional[str] = None) -> dict:
    """One pass: claim what is due (in `project_id` only, when given), process each row in its
    own transaction."""
    from ..db import background, begin

    with background():
        settings = settings or _settings()
        with begin(engine) as c:
            # One clock for the claim and for the backoff written on the rows it takes.
            now = _due_now(c, now)
            rows = claim(c, limit=limit, holder=holder, now=now, project_id=project_id)
        out = {"claimed": len(rows), "routed": 0, "ignored": 0, "filtered": 0, "refused": 0,
               "failed": 0, "dead": 0, "received": 0, "deferred": 0, "digest": 0}
        for row in rows:
            got = process_one(engine, settings, row, now=now)
            out[got["status"]] = out.get(got["status"], 0) + 1
        return out


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The poller (BOSSAPP-1175): sources BOSS reads, for an install nobody can reach
# ═══════════════════════════════════════════════════════════════════════════════════════════════

#: `poll_seconds` on a connection: how often it is polled (GitHub: 2-4 conditional requests a
#: poll, and a 304 is free, so 30 s stays far inside 5,000 requests an hour).
POLL_SECONDS_DEFAULT = 30
POLL_SECONDS_MIN = 15
#: The longest a failing connection waits between polls.
POLL_BACKOFF_MAX_SECONDS = 900
POLL_STREAM_KEY = "poll"
#: A cursor not advanced for this long belongs to a connection that was dormant (paused, or
#: its workers down): its next poll is a fresh BASELINE at now, not a replay of everything
#: that moved meanwhile.
POLL_DORMANT_SECONDS = 3600


def poll_seconds(config: Mapping[str, Any]) -> int:
    raw_val = (config or {}).get("poll_seconds") or (config or {}).get("poll_interval_seconds") or POLL_SECONDS_DEFAULT
    try:
        wanted = int(str(raw_val).strip())
    except ValueError:
        wanted = POLL_SECONDS_DEFAULT
    return max(POLL_SECONDS_MIN, wanted)


def _insert_polled(connection: Connection, row: Mapping[str, Any], raw: StoredRaw,
                   received: datetime, *, verify_scheme: str = "poll") -> Optional[str]:
    """One polled or streamed delivery into the inbox, exactly as the front door inserts a pushed
    one: deduplicated on (connection, dedupe_key), announced to the processor at commit."""
    event_id = make_id("iev")
    dk = raw.dedupe_key[:500]
    claimed = connection.execute(text("""
        INSERT INTO intake_event_dedupe (connection_id, dedupe_key, event_id, received_at)
        VALUES (:c, :dk, :id, :at)
        ON CONFLICT (connection_id, dedupe_key) DO NOTHING RETURNING event_id"""),
        {"c": row["id"], "dk": dk, "id": event_id, "at": received}).scalar()
    if claimed is None:
        return None
    connection.execute(text("""
        INSERT INTO intake_events (id, connection_id, project_id, dedupe_key, source_event_id,
               status, raw, raw_sha256, headers, trace_id, verify_scheme, received_at,
               raw_expires_at, expires_at)
        VALUES (:id, :c, :p, :dk, :sid, 'received', :raw, :sha, CAST(:h AS JSONB), :tr, :vs,
                :at, :rexp, :exp)"""),
        {"id": event_id, "c": row["id"], "p": row.get("project_id"),
         "dk": dk, "sid": dk.split(":", 1)[-1][:200],
         "raw": raw.body, "sha": hashlib.sha256(raw.body).hexdigest(),
         "h": json.dumps(kept_headers(raw.headers)), "tr": make_id("itr"), "vs": verify_scheme,
         "at": received, "rexp": received + timedelta(days=RAW_RETENTION_DAYS),
         "exp": received + timedelta(days=ENVELOPE_RETENTION_DAYS)})
    return event_id


def poll_due(engine: Any, settings: Any = None, *, now: Optional[datetime] = None,
             holder: str = HOLDER, connection_id: Optional[str] = None) -> dict:
    """Poll every active `poll` connection that is due. ONE POLLER PER CONNECTION: its cursor
    row is locked `FOR UPDATE SKIP LOCKED` for the poll, so two workers never poll one
    connection (the other skips it), and a crash releases the lock with the transaction. The
    deliveries and the advanced cursor commit together: a crash re-reads one poll, and the
    inbox's dedupe key absorbs the repeat. A refusal or a rate limit backs off (doubling, at
    most 15 minutes, or the source's own Retry-After) and is written on the connection's
    health; the token is never written anywhere."""
    from ..db import begin
    from ..connectors.intake.contract import AuthFailed, Cursor, RateLimited

    settings = settings or _settings()
    now = now or _now()
    out = {"polled": 0, "inserted": 0, "skipped": 0, "failed": 0}
    with begin(engine) as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT * FROM intake_connections WHERE mode IN ('poll', 'push') AND status = 'active' "
            "AND (CAST(:only AS TEXT) IS NULL OR id = :only)"), {"only": connection_id}
        ).mappings()]
    for crow in rows:
        adapter = REGISTRY.get(str(crow["kind"]))
        if adapter is None or not adapter.can("poll"):
            continue
        if crow["mode"] == "push":
            probe_conn = IntakeConnection(
                str(crow["id"]), str(crow.get("project_id") or ""), str(crow["kind"]),
                mode="push", config=_json(crow.get("config"), {}) or {},
            )
            checker = getattr(adapter, "wants_safety_net_poll", None)
            if callable(checker):
                if not checker(probe_conn):
                    continue
            elif not probe_conn.get("poll_safety_net"):
                continue
        health = _json(crow.get("health"), {}) or {}
        state = dict(health.get("poll") or {})
        due = _event_time(state.get("next_poll_at"))
        if due is not None and due > now:
            out["skipped"] += 1
            continue
        interval = poll_seconds(_json(crow.get("config"), {}) or {})
        with begin(engine) as c:
            c.execute(text("""
                INSERT INTO intake_cursors (connection_id, stream_key, cursor, cursor_time)
                VALUES (:c, :k, NULL, :since) ON CONFLICT DO NOTHING"""),
                {"c": crow["id"], "k": POLL_STREAM_KEY, "since": now})
        with begin(engine) as c:
            cur = c.execute(text("""
                SELECT * FROM intake_cursors WHERE connection_id = :c AND stream_key = :k
                   FOR UPDATE SKIP LOCKED"""),
                {"c": crow["id"], "k": POLL_STREAM_KEY}).mappings().first()
            if cur is None:
                out["skipped"] += 1          # another worker is polling this connection now
                continue
            # DUE IS DECIDED UNDER THE LOCK: another worker may have polled between the read
            # above and taking the lock.
            crow = dict(c.execute(text("SELECT * FROM intake_connections WHERE id = :c"),
                                  {"c": crow["id"]}).mappings().first() or crow)
            state = dict((_json(crow.get("health"), {}) or {}).get("poll") or {})
            due = _event_time(state.get("next_poll_at"))
            if due is not None and due > now:
                out["skipped"] += 1
                continue
            conn = to_intake_connection(c, settings, crow)
            last = _event_time(cur.get("updated_at"))
            if cur.get("cursor") and last is not None and \
                    (now - last).total_seconds() > POLL_DORMANT_SECONDS:
                cursor = Cursor(stream_key=POLL_STREAM_KEY, value=None,
                                cursor_time=now.isoformat())      # dormant: a new baseline
            else:
                cursor = Cursor(stream_key=POLL_STREAM_KEY, value=cur.get("cursor"),
                                cursor_time=_iso(cur.get("cursor_time")))
            try:
                with c.begin_nested():
                    result = adapter.poll(conn, cursor, PROCESSOR_BATCH)
            except Exception as error:  # noqa: BLE001 — written on the connection, backed off
                failures = int(state.get("failures") or 0) + 1
                wait = min(POLL_BACKOFF_MAX_SECONDS, interval * (2 ** min(failures, 6)))
                if isinstance(error, RateLimited) and error.retry_after:
                    wait = max(wait, int(error.retry_after))
                kind = ("rate_limited" if isinstance(error, RateLimited) else
                        "auth_failed" if isinstance(error, AuthFailed) else "error")
                state.update(failures=failures, last_error=f"{kind}: {error}"[:300],
                             last_poll_at=now.isoformat(),
                             next_poll_at=(now + timedelta(seconds=wait)).isoformat(),
                             holder=holder[:80])
                c.execute(text("UPDATE intake_cursors SET last_error = :e "
                               "WHERE connection_id = :c AND stream_key = :k"),
                          {"e": state["last_error"], "c": crow["id"], "k": POLL_STREAM_KEY})
                c.execute(text("UPDATE intake_connections SET health = health || "
                               "CAST(:h AS JSONB) WHERE id = :id"),
                          {"h": json.dumps({"poll": state}), "id": crow["id"]})
                out["failed"] += 1
                continue
            inserted = 0
            for raw in result.deliveries:
                if _insert_polled(c, crow, raw, now):
                    inserted += 1
            nxt = result.next_cursor
            c.execute(text("""
                UPDATE intake_cursors SET cursor = :v, cursor_time = COALESCE(:t, cursor_time),
                       last_error = NULL, resync_required = false, updated_at = :now
                 WHERE connection_id = :c AND stream_key = :k"""),
                {"v": nxt.value, "t": _event_time(nxt.cursor_time), "c": crow["id"],
                 "k": POLL_STREAM_KEY, "now": now})
            state = {"failures": 0, "last_error": None, "last_poll_at": now.isoformat(),
                     "next_poll_at": (now + timedelta(seconds=interval)).isoformat(),
                     "last_found": len(result.deliveries), "last_inserted": inserted,
                     "total_inserted": int(state.get("total_inserted") or 0) + inserted,
                     "interval_seconds": interval, "holder": holder[:80]}
            c.execute(text("UPDATE intake_connections SET health = health || CAST(:h AS JSONB) "
                           "WHERE id = :id"), {"h": json.dumps({"poll": state}), "id": crow["id"]})
            out["polled"] += 1
            out["inserted"] += inserted
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


# ── who a chat user is in BOSS: one redaction for every intake read (BOSSAPP-1196) ──────────

#: A key whose value ties a chat identity to a BOSS person, or is an address: WHOLE keys after
#: normalising (camelCase and dashes → snake_case), never a part of one — `principal_amount`,
#: `mail_server` and `email_template` are content and stay. The principal (`principal`,
#: `principal_id`, `*_principal_id`), who acted for whom (`person_actor`, `on_behalf_of`,
#: `acting`), and an address (`email`, `emails`, `email_address`, `*_email`, `*_emails`,
#: `*_email_address`, `mail`, `*_mail`, `upn`, `*_upn`, `user_principal_name`).
_PERSON_KEYS = frozenset({"principal", "principal_id", "person_actor", "on_behalf_of", "acting",
                          "email", "emails", "email_address", "email_addresses", "mail", "upn",
                          "user_principal_name"})
_PERSON_KEY_SUFFIXES = ("_principal_id", "_email", "_emails", "_email_address", "_mail", "_upn")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_ADDRESS = (r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*"
            r"\.[A-Za-z]{2,63}")
#: An email address in a string value, with its wrapping (`<a@b>`, Slack's
#: `<mailto:a@b|a@b>`). Not a URL's userinfo or an scp-style remote: the local part may not
#: follow `/`, `:` or a word character (`https://token@github.com/…`), and the host may not be
#: followed by `/`, `:<path>` or more host (`git@github.com:o/r.git`).
_EMAIL_IN_TEXT = re.compile(
    rf"(?:<?mailto:|<|(?<![\w/:.%+\-@])){_ADDRESS}(?:\|{_ADDRESS})?>?"
    r"(?![\w\-@/]|[.:][\w/])")


def _snake(key: Any) -> str:
    return _CAMEL.sub("_", str(key)).replace("-", "_").replace(" ", "_").lower()


def _person_key(key: Any) -> bool:
    k = _snake(key)
    return k in _PERSON_KEYS or k.endswith(_PERSON_KEY_SUFFIXES)


def _scrub_text(value: str) -> Optional[str]:
    """A string with every email address cut out; None when the address was all it held."""
    if not _EMAIL_IN_TEXT.search(value):
        return value
    left = _EMAIL_IN_TEXT.sub("", value)
    return left if left.strip(" <>\"'()[]:;,|") else None


def redact_people(value: Any) -> Any:
    """A copy of `value` with every person-linking field (`_person_key`) set to None and every
    email address in any string value cut out, at any depth: dicts, lists, the envelope, the
    raw body, each outcome's subject. What a caller without `user:read` gets from every intake
    read (events, one event, dead letters, the route preview, the stream search). A READ VIEW
    only: what an agent receives is never passed through it (agent_triggers._agent_safe).
    Nothing else changes: the chat user's own id and display stay, URLs stay whole here (their
    credentials are masked for every caller by `mask_credentials`, BOSSAPP-1202)."""
    if isinstance(value, Mapping):
        return {k: (None if _person_key(k) and v is not None else redact_people(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_people(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def mask_credentials(value: Any) -> Any:
    """`value` with every URL's userinfo credential masked (`redaction.mask_url_credentials`:
    `https://user:***@host`), at any depth. Applied to EVERY intake read for EVERY role, Owner
    included (BOSSAPP-1202): a credential is never shown. A READ VIEW only, like `redact_people`:
    what an agent receives keeps the URL whole (agent_triggers._agent_safe)."""
    return mask_url_credentials_in(value)


def _mask_raw_base64(encoded: Optional[str]) -> Optional[str]:
    """A body kept as base64 (not JSON) that is still UTF-8 text is masked like any string and
    re-encoded; one that is not text has no URL to mask and stays as it is."""
    if not encoded:
        return encoded
    try:
        body = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return encoded
    masked = mask_url_credentials(body)
    return encoded if masked == body else base64.b64encode(masked.encode("utf-8")).decode("ascii")


def _redacted(out: dict, reveal: bool) -> dict:
    """`out` as the caller may see it, flagged (`people_redacted`) for the console's "name
    hidden". Fails closed: `reveal` must be exactly True to show the person. URL credentials
    are masked for every caller, `reveal` or not (`mask_credentials`)."""
    out = mask_credentials(out)
    if out.get("raw_base64") is not None:
        out["raw_base64"] = _mask_raw_base64(out["raw_base64"])
    if reveal is True:
        return {**out, "people_redacted": False}
    out = redact_people(out)
    if out.get("raw_base64") is not None:
        out["raw_base64"] = None          # an opaque body cannot be scrubbed: withheld whole
    out["people_redacted"] = True
    return out


def _searchable(row: Mapping[str, Any], reveal: bool = False) -> str:
    """What `q` reads: the searched columns and the envelope AFTER the read view's redaction —
    URL credentials masked for everyone (`mask_credentials`), people withheld without `reveal`
    (`redact_people`) — so a search is never an oracle for a hidden field (a password) and the
    search and the answer cannot drift."""
    shown = mask_credentials({"type": row.get("type"), "subject": row.get("subject"),
                              "dedupe_key": row.get("dedupe_key"),
                              "actor_external_id": row.get("actor_external_id"),
                              "envelope": _json(row.get("envelope"), {})})
    if reveal is not True:
        shown = redact_people(shown)
    return json.dumps(shown, default=str, ensure_ascii=False).casefold()


#: How many candidate rows a redacted search reads before it stops looking for more matches.
_SEARCH_SCAN_MAX = 5000


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
                before: Optional[str] = None, reveal: bool = False) -> dict:
    """The stream (E5): newest first, with status chips; `q` searches type, subject, actor,
    the envelope's text and the dedupe key. Without `reveal` (user:read) every person-linking
    field is withheld (`redact_people`) and `q` searches only what is left (`_searchable`)."""
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
        # SQL narrows to the rows that match anywhere (a superset); each one is then re-checked
        # against its redacted form (`_searchable`), page by page — for every caller, since a
        # URL credential is masked even for one who may see people (BOSSAPP-1202).
        where.append("(type ILIKE :q OR subject ILIKE :q OR dedupe_key ILIKE :q OR "
                     "actor_external_id ILIKE :q OR envelope::text ILIKE :q)")
        params["q"] = f"%{str(q)[:200]}%"
    sql = (f"SELECT {_EVENT_COLUMNS} FROM intake_events WHERE {' AND '.join(where)} "
           "{after} ORDER BY received_at DESC, id DESC LIMIT :n")
    search: dict[str, Any] = {}
    if not q:
        rows = connection.execute(text(sql.format(after="")), params).mappings().all()
    else:
        needle = str(q)[:200].casefold()
        rows, scanned, after, exhausted = [], 0, "", False
        while len(rows) < params["n"] and scanned < _SEARCH_SCAN_MAX:
            page = connection.execute(text(sql.format(after=after)), params).mappings().all()
            scanned += len(page)
            rows.extend(r for r in page if needle in _searchable(r, reveal))
            if len(page) < params["n"]:
                exhausted = True
                break
            last = page[-1]
            after = "AND (received_at, id) < (:after_at, :after_id)"
            params.update({"after_at": last["received_at"], "after_id": last["id"]})
        rows = rows[:params["n"]]
        # Said, never silent: the scan stopped at its cap with candidates left unread.
        search = {"scanned": scanned,
                  "search_truncated": not exhausted and len(rows) < params["n"]
                  and scanned >= _SEARCH_SCAN_MAX,
                  "search_limit": _SEARCH_SCAN_MAX}
    conns = _connections_of(connection, project_id)
    return _redacted({"events": [_summary(dict(r), conns) for r in rows],
                      "counts": status_counts(connection, project_id,
                                              since_hours=since_hours or 24), **search}, reveal)


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
              include_raw: bool = True, reveal: bool = False) -> Optional[dict]:
    """One event in full (E5's expanded row): envelope, guards, outcome, the raw body (decoded
    JSON when it is JSON and small) and the fires it caused. Without `reveal` (user:read) the
    BOSS person behind the chat user (principal_id, emails) is withheld (`redact_people`)."""
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
    return _redacted(out, reveal)


def dead_letters(connection: Connection, project_id: str, *, limit: int = 100,
                 connection_id: Optional[str] = None, reveal: bool = False) -> dict:
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
    return _redacted({"dead_letters": items,
                      "per_connection": sorted(per.values(), key=lambda x: x["name"])}, reveal)


# ── people links (E7) ──────────────────────────────────────────────────────────────────────────

#: A revoked link stays on the People screen this long, so an admin sees what was just undone.
REVOKED_SHOWN_DAYS = 30

_GUEST_KEYS = ("is_restricted", "is_ultra_restricted", "user_is_restricted", "is_stranger",
               "is_external", "is_guest")


def _guest(data: Mapping[str, Any], cfg: Mapping[str, Any]) -> bool:
    """The guest rule of intake_identity.is_guest, on a stored envelope's `data`: a Slack guest,
    a Slack Connect user from another workspace, a Teams guest or another tenant."""
    if any(data.get(k) is True for k in _GUEST_KEYS):
        return True
    if str(data.get("user_type") or data.get("userType") or "").lower() == "guest":
        return True
    team, own = data.get("user_team_id") or data.get("team_id"), cfg.get("team_id")
    if team and own and str(team) != str(own):
        return True
    tenant, own_tenant = data.get("tenant_id") or data.get("tenantId"), cfg.get("tenant_id")
    return bool(tenant and own_tenant and str(tenant) != str(own_tenant))


def _display_of(envelope: Any) -> str:
    env = _json(envelope, {}) or {}
    return str((env.get("bossactor") or {}).get("display") or "")


#: At most this many chat users on one page of the People screen (`offset` pages on).
PEOPLE_PAGE = 200
PEOPLE_PAGE_MAX = 1000


def connection_people(connection: Connection, *, project_id: str, connection_id: str,
                      reveal: bool = False, limit: int = PEOPLE_PAGE, offset: int = 0) -> dict:
    """E7: who each chat user of this connection is in BOSS. Linked people (method code /
    sso_email / aad_oid, when), links revoked in the last 30 days, and the chat users BOSS has
    heard from who are not linked (guests flagged: never auto-linked), with when a link prompt
    really went to them (the outbox's own `link_dm` row). Plus the connection's SSO attestation.
    Never a link code.

    WHO THE BOSS PERSON IS (name, email, principal) and the chat user's email are directory
    data: only a caller holding `user:read` gets them (`reveal`); everyone else sees the link,
    its method and when, with `person` withheld (`people_redacted: true`)."""
    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None:
        raise IntakeServiceError("no such connection in this project.", status=404, code="not_found")
    limit = max(1, min(int(limit or PEOPLE_PAGE), PEOPLE_PAGE_MAX))
    offset = max(0, int(offset or 0))
    cfg = _json(row.get("config"), {}) or {}
    endpoint_id = row.get("chat_endpoint_id")
    counts = {"linked": 0, "not_linked": 0, "guests": 0, "revoked": 0}
    out: dict[str, Any] = {"connection_id": connection_id, "kind": row["kind"],
                           "endpoint_id": endpoint_id, "attestation": cfg.get(ATTESTATION_KEY),
                           "people": [], "counts": counts, "people_redacted": not reveal,
                           "limit": limit, "offset": offset, "next_offset": None}
    if not endpoint_id:
        return out
    links = connection.execute(text("""
        SELECT l.id, l.external_user_id, l.external_name, l.external_email, l.method,
               l.verified_at, l.revoked_at, l.principal_id,
               p.display_name AS person_name, u.email AS person_email
          FROM chat_identity_links l
          LEFT JOIN identity_principals p ON p.id = l.principal_id
          LEFT JOIN identity_users u ON u.principal_id = l.principal_id
         WHERE l.endpoint_id = :e AND l.verified_at IS NOT NULL
           AND (l.revoked_at IS NULL OR l.revoked_at > now() - make_interval(days => :d))
         ORDER BY l.revoked_at NULLS FIRST, l.verified_at DESC"""),
        {"e": endpoint_id, "d": REVOKED_SHOWN_DAYS}).mappings().all()
    # The chat users heard from, newest first (idx_intake_events_connection_actor).
    last_seen = {str(r["actor_external_id"]): r for r in connection.execute(text("""
        SELECT * FROM (
          SELECT DISTINCT ON (actor_external_id) actor_external_id, envelope, received_at
            FROM intake_events
           WHERE connection_id = :c AND actor_kind = 'user' AND actor_external_id IS NOT NULL
             AND actor_external_id <> ''
           ORDER BY actor_external_id, received_at DESC) seen
         ORDER BY received_at DESC LIMIT :n"""),
        {"c": connection_id, "n": PEOPLE_PAGE_MAX * 5}).mappings().all()}
    # A link prompt is the outbox's `link_dm:intake:<endpoint>:<user>:<hour>` row, nothing else
    # (a refused event may have been refused for a budget, a grant, a stop).
    prompted = {str(r["user_id"]): r["at"] for r in connection.execute(text("""
        SELECT split_part(dedupe_key, ':', 4) AS user_id, max(created_at) AS at
          FROM chat_outbox WHERE endpoint_id = :e AND dedupe_key LIKE :k
         GROUP BY 1"""), {"e": endpoint_id, "k": f"link_dm:intake:{endpoint_id}:%"}).mappings().all()}
    listed: set[str] = set()
    people: list[dict] = []
    for r in links:
        uid = str(r["external_user_id"])
        if uid in listed:
            continue                      # a revoked link under a newer live one: show the live one
        listed.add(uid)
        state = "revoked" if r["revoked_at"] is not None else "linked"
        heard = last_seen.get(uid)
        people.append({
            "external_user_id": uid,
            "display": r["external_name"] or (_display_of(heard["envelope"]) if heard else ""),
            "state": state, "link_id": r["id"], "method": r["method"] or "code",
            "person": ({"principal_id": r["principal_id"], "name": r["person_name"] or "",
                        "email": r["person_email"] or r["external_email"] or ""}
                       if r["principal_id"] and reveal else None),
            "linked_at": _iso(r["verified_at"]), "revoked_at": _iso(r["revoked_at"]),
            "last_seen_at": _iso(heard["received_at"]) if heard else None,
            "guest": False, "prompted_at": None})
        counts["revoked" if state == "revoked" else "linked"] += 1
    for uid, r in last_seen.items():
        if uid in listed:
            continue
        guest = _guest((_json(r["envelope"], {}) or {}).get("data") or {}, cfg)
        people.append({
            "external_user_id": uid, "display": _display_of(r["envelope"]),
            "state": "guest" if guest else "not_linked", "link_id": None, "method": None,
            "person": None, "linked_at": None, "revoked_at": None,
            "last_seen_at": _iso(r["received_at"]), "guest": guest,
            "prompted_at": None if guest else _iso(prompted.get(uid))})
        counts["guests" if guest else "not_linked"] += 1
    out["people"] = people[offset:offset + limit]
    out["next_offset"] = offset + limit if len(people) > offset + limit else None
    return out


def revoke_person_link(connection: Connection, *, project_id: str, connection_id: str,
                       link_id: str, actor: str) -> dict:
    """An admin ends one chat user's link to a BOSS person. Their next message gets a new link
    prompt; an SSO auto-link is not silently remade (intake_identity skips a revoked pair)."""
    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None or not row.get("chat_endpoint_id"):
        raise IntakeServiceError("no such chat connection in this project.", status=404,
                                 code="not_found")
    done = connection.execute(text(
        "UPDATE chat_identity_links SET revoked_at = now(), updated_at = now() "
        "WHERE id = :l AND endpoint_id = :e AND revoked_at IS NULL "
        "RETURNING id, external_user_id, method"),
        {"l": link_id, "e": row["chat_endpoint_id"]}).mappings().first()
    if done is None:
        raise IntakeServiceError("no such live link on this connection.", status=404,
                                 code="not_found")
    emit_event(connection, project_id, "chat.link_revoked", actor, "intake_connection",
               connection_id, {"link_id": link_id, "external_user_id": done["external_user_id"],
                               "method": done["method"], "by_admin": True})
    return {"revoked": link_id, "external_user_id": done["external_user_id"]}


def _prompt_event(connection_id: str, user: str, display: str) -> IntakeEvent:
    return IntakeEvent(id=f"prompt:{user}", source="intake://prompt", type="com.slack.message",
                       bossconnection=connection_id,
                       bossactor=ExternalActor(kind="user", external_id=user, display=display))


def prompt_people(connection: Connection, settings: Any, *, project_id: str, connection_id: str,
                  external_user_id: Optional[str] = None, resend: bool = False) -> dict:
    """Send a link prompt to every not-linked person on this connection, or to one of them.
    Guests are never prompted. A second bulk send in the same hour does not send again; Resend
    (`external_user_id` and `resend`) forces a new code. The answer never includes a link code."""
    from . import intake_identity

    row = get_connection(connection, connection_id, project_id=project_id)
    if row is None or not row.get("chat_endpoint_id"):
        raise IntakeServiceError("no such chat connection in this project.", status=404,
                                 code="not_found")
    page = connection_people(connection, project_id=project_id, connection_id=connection_id,
                             limit=PEOPLE_PAGE_MAX)
    people = {str(p["external_user_id"]): p for p in page["people"]}

    def one(person: Mapping[str, Any], *, force: bool) -> bool:
        return intake_identity.prompt_link(
            connection, settings, row,
            _prompt_event(connection_id, str(person["external_user_id"]), str(person.get("display") or "")),
            force=force)

    if external_user_id:
        person = people.get(external_user_id)
        if person is None:
            raise IntakeServiceError("no such person on this connection.", status=404, code="not_found")
        if person.get("guest") or person.get("state") == "guest":
            raise IntakeServiceError("Guests are never sent a link prompt.", status=422, code="guest")
        if person.get("state") == "linked":
            raise IntakeServiceError("That person is already linked.", status=422, code="already_linked")
        if person.get("state") != "not_linked":
            raise IntakeServiceError("Only a person who is not linked can be prompted.", status=422,
                                     code="not_promptable")
        sent = one(person, force=bool(resend))
        return {"sent": [external_user_id] if sent else [],
                "already_sent": [] if sent else [external_user_id], "skipped": []}
    sent: list[str] = []
    already: list[str] = []
    for person in page["people"]:
        if person.get("state") != "not_linked" or person.get("guest"):
            continue
        (sent if one(person, force=False) else already).append(str(person["external_user_id"]))
    return {"sent": sent, "already_sent": already, "skipped": []}


def sweep_retention(connection: Connection, *, now: Optional[datetime] = None) -> dict:
    """§4.5 + P9c-3 (BOSSAPP-1107): raw bodies are nulled after 14 days (the hash stays); rows go
    after 30 days (routed/ignored/...) or 90 days (dead/refused); monthly partitions are pre-created
    and expired monthly partitions dropped; a row still being worked is never touched."""
    from . import intake_retention

    res = intake_retention.sweep_retention(connection, now=now)
    return {"raw_nulled": int(res["raw_nulled"]), "deleted": int(res["deleted"])}


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
        from ..db import background, begin
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
                with background():
                    try:
                        poll_due(engine, settings)
                    except Exception:  # noqa: BLE001 — a failed poll never stops processing
                        logger.warning("intake poll failed", exc_info=True)
                    try:
                        from . import intake_subscriptions

                        intake_subscriptions.renew_due(engine, settings)
                    except Exception:  # noqa: BLE001 — a failed renewal never stops processing
                        logger.warning("intake subscription renew failed", exc_info=True)
                    try:
                        from . import intake_streams

                        intake_streams.consume_due(engine, settings)
                    except Exception:  # noqa: BLE001 — a failed stream never stops processing
                        logger.warning("intake stream consume failed", exc_info=True)
                    got = process_due(engine, settings)
                    while got["claimed"] >= PROCESSOR_BATCH and not _STOP.is_set():
                        got = process_due(engine, settings)
                    flush_refusals(engine)
                    try:
                        from . import intake_guards

                        intake_guards.sweep_digests(engine)
                    except Exception:  # noqa: BLE001 — the next tick tries again
                        logger.debug("intake digest sweep failed", exc_info=True)
                    if time.monotonic() - last_sweep > 60:
                        last_sweep = time.monotonic()
                        with begin(engine) as c:
                            sweep_retention(c)
                    system_services.beat(engine, SERVICE_KEY, ok=True)
            except Exception as error:  # noqa: BLE001 — the next tick tries again
                logger.warning("intake processor tick failed", exc_info=True)
                try:
                    with background():
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
    "SERVICE_KEY", "SHED_RAW_BYTES", "accept", "flush_refusals",
    "ATTESTATION_KEY", "IntakeServiceError", "backoff_seconds", "clear_sso_attestation",
    "set_sso_attestation", "claim", "connection_by_token", "create_connection",
    "connection_people", "dead_letters", "delete_connection", "describe_connection",
    "get_connection", "get_event", "inbound_path", "list_connections", "list_events", "poll_due",
    "poll_seconds", "process_due", "process_one", "receive", "revoke_person_link",
    "replay", "rotate_secret", "set_connection_status", "start", "status_counts", "stop",
    "sweep_retention", "test_connection", "to_intake_connection", "update_connection",
]
