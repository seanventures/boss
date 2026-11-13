"""THE HEADER AND METADATA POLICY OF AN MCP SERVER (BOSSAPP-873).

Before this, an upstream MCP call carried the server's sign-in (mcp_registry.auth_headers: its
bearer / header token or the caller's OAuth credential), any plain static headers typed on its
auth, and BOSS's own protocol headers (MCPClient._post). Nothing could say "this internal server
routes tenants by X-Tenant-Key, which is a secret", "tag every call with the run and agent so the
upstream audit can be joined to ours", or "send nothing else".

``mcp_servers.headers_policy`` says it, per server. Empty ({}) is the default and changes nothing.

  allow           [names]  An ALLOWLIST for the extra headers: only these go upstream, with the
                           server's sign-in header and BOSS's protocol headers (Accept,
                           Content-Type, Mcp-Session-Id, MCP-Protocol-Version), which always go.
                           Any other — a static header typed earlier, a header added on another
                           road — is stripped. Absent: no stripping (today's behaviour).
  secret_headers  {name: {secret_ref_id, last4}}
                           Static headers whose VALUES are secrets: written through to the secret
                           store when set, resolved only at call time, shown as last four.
  forward         [run_id | agent | traceparent]
                           OPT-IN per-call metadata on tools/call: X-BOSS-Run-Id, X-BOSS-Agent and
                           the W3C ``traceparent`` of the current span (only when BOSS's own
                           tracing is on, BOSSAPP-867). Sent on the call, never on the session's
                           initialize, so they never split one server's session per run.

Every header still passes identity.tokens.refuse_passthrough in MCPClient: a policy can never send
a BOSS session, key or run token upstream.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The per-call metadata a server may opt into, and the header each one is sent as.
FORWARD = {"run_id": "X-BOSS-Run-Id", "agent": "X-BOSS-Agent", "traceparent": "traceparent"}
#: Headers BOSS itself sets on every MCP request (MCPClient._post); never stripped, never set here.
PROTOCOL = {"accept", "content-type", "mcp-session-id", "mcp-protocol-version",
            "content-length", "host"}
_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_HEADERS = 20


class HeadersPolicyError(ValueError):
    """A policy that cannot be stored as written. The API answers 422 with the sentence."""


def _loads(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _name(raw: Any) -> str:
    name = str(raw or "").strip()
    low = name.lower()
    if not _NAME.match(name) or low in PROTOCOL or low == "authorization" \
            or low in {v.lower() for v in FORWARD.values()}:
        raise HeadersPolicyError(f"header {name!r} cannot be set by a policy.")
    return name


def validate(policy: Any) -> dict:
    """A policy as given (``secret_headers`` values may be plain text to store, or kept refs),
    checked and normalised. Raises HeadersPolicyError with one sentence."""
    if policy in (None, ""):
        return {}
    if not isinstance(policy, Mapping):
        raise HeadersPolicyError("a headers policy is {allow, secret_headers, forward}.")
    unknown = set(policy) - {"allow", "secret_headers", "forward"}
    if unknown:
        raise HeadersPolicyError(f"unknown headers policy field(s): {', '.join(sorted(unknown))}.")
    out: dict[str, Any] = {}
    if policy.get("allow") is not None:
        allow = policy["allow"]
        if not isinstance(allow, list) or len(allow) > MAX_HEADERS:
            raise HeadersPolicyError(f"allow is a list of at most {MAX_HEADERS} header names.")
        out["allow"] = sorted({_name(n) for n in allow}, key=str.lower)
    secrets = policy.get("secret_headers") or {}
    if not isinstance(secrets, Mapping) or len(secrets) > MAX_HEADERS:
        raise HeadersPolicyError(f"secret_headers maps at most {MAX_HEADERS} names to values.")
    if secrets:
        clean: dict[str, Any] = {}
        for raw, value in secrets.items():
            name = _name(raw)
            if isinstance(value, Mapping):
                if not (value.get("secret_ref_id") or value.get("keep")
                        or str(value.get("secret") or "").strip()):
                    raise HeadersPolicyError(f"{name} needs a value.")
                clean[name] = dict(value)
            elif isinstance(value, str) and value.strip():
                clean[name] = {"secret": value.strip()}
            else:
                raise HeadersPolicyError(f"{name} needs a value.")
        out["secret_headers"] = clean
    forward = policy.get("forward") or []
    if not isinstance(forward, list) or any(f not in FORWARD for f in forward):
        raise HeadersPolicyError("forward lists run_id, agent and/or traceparent.")
    if forward:
        out["forward"] = [f for f in FORWARD if f in forward]
    return out


def stored(connection: Any, settings: Any, project_id: str, server_name: str, policy: Mapping,
           *, actor: str, keep: Optional[Mapping] = None) -> dict:
    """What goes in the column: each new secret header value written THROUGH to the store and
    replaced by its reference and last four; a header given as ``{keep: true}`` (or with its
    existing ``secret_ref_id``) keeps the value already stored."""
    out = {k: v for k, v in policy.items() if k != "secret_headers"}
    kept = (_loads(keep).get("secret_headers") or {}) if keep else {}
    secrets: dict[str, Any] = {}
    for name, value in (policy.get("secret_headers") or {}).items():
        if value.get("secret"):
            from .secret_backends import default_secret_backend
            from .secrets_service import put_secret

            written = put_secret(connection, settings, project_id=project_id,
                                 name=f"mcp_{server_name}_header_{name.lower()}",
                                 value=str(value["secret"]), secret_kind="api_token",
                                 backend=default_secret_backend(settings), actor=actor)
            secret = str(value["secret"])
            secrets[name] = {"secret_ref_id": written["secret_ref_id"],
                             "last4": written.get("last4") or (secret[-4:] if len(secret) >= 8
                                                               else None)}
        elif value.get("secret_ref_id") or value.get("keep"):
            ref = value.get("secret_ref_id") or (kept.get(name) or {}).get("secret_ref_id")
            if not ref:
                raise HeadersPolicyError(f"{name} has no stored value to keep; type it again.")
            if ref != (kept.get(name) or {}).get("secret_ref_id"):
                # A reference is only ever kept, never pointed at another secret by a caller.
                raise HeadersPolicyError(f"{name}: a stored value can be kept, not swapped.")
            secrets[name] = {"secret_ref_id": ref, "last4": (kept.get(name) or {}).get("last4")}
    if secrets:
        out["secret_headers"] = secrets
    return out


def set_policy(connection: Any, settings: Any, project_id: str, server: Mapping[str, Any],
               policy: Any, *, actor: str) -> dict:
    """Validate, write through any new secret values, store, and record
    ``mcp.headers_policy_set`` (names only). Answers the public policy."""
    from .core import emit_event

    checked = validate(policy)
    server_id = str(server.get("id") or "")
    keep = read(connection, project_id, server_id)
    value = stored(connection, settings, project_id, str(server.get("name") or server_id),
                   checked, actor=actor, keep=keep)
    connection.execute(text(
        "UPDATE mcp_servers SET headers_policy = CAST(:hp AS JSONB), updated_at = now() "
        "WHERE id = :id AND project_id = :p"),
        {"hp": json.dumps(value), "id": server_id, "p": project_id})
    shown = public(value)
    emit_event(connection, project_id, "mcp.headers_policy_set", actor, "mcp_server", server_id,
               {"allow": shown["allow"], "forward": shown["forward"],
                "secret_headers": sorted(shown["secret_headers"])})
    return shown


def public(policy: Any) -> dict:
    """The policy a screen may see: names, last four, never a reference or a value."""
    p = _loads(policy)
    out: dict[str, Any] = {"allow": p.get("allow"), "forward": list(p.get("forward") or []),
                           "secret_headers": {n: {"set": True, "last4": (v or {}).get("last4")}
                                              for n, v in (p.get("secret_headers") or {}).items()}}
    return out


def read(connection: Any, project_id: str, server_id: Optional[str]) -> dict:
    """A server's stored policy; {} for none, an unknown server or a store without the column."""
    if not server_id:
        return {}
    try:
        with connection.begin_nested():
            found = connection.execute(
                text("SELECT headers_policy FROM mcp_servers WHERE id = :id AND project_id = :p"),
                {"id": server_id, "p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — an install that predates the column has no policy
        return {}
    return _loads(found)


def apply(connection: Any, settings: Any, policy: Mapping, headers: Mapping[str, str], *,
          sign_in: set[str]) -> dict[str, str]:
    """The headers a call sends with this policy: the secret headers resolved NOW and added
    (never over a sign-in header), then — when ``allow`` is set — every header that is not the
    sign-in, a protocol header or allowed, stripped. ``sign_in`` names the server's own sign-in
    headers (lower case)."""
    out = {str(k): str(v) for k, v in headers.items()}
    secrets = policy.get("secret_headers") or {}
    if secrets:
        from .secret_backends import resolve_secret_ref

        for name, ref in secrets.items():
            if name.lower() in sign_in or not (ref or {}).get("secret_ref_id"):
                continue
            out[name] = resolve_secret_ref(connection, str(ref["secret_ref_id"]), settings)
    allow = policy.get("allow")
    if allow is not None:
        keep = {a.lower() for a in allow} | sign_in | PROTOCOL
        out = {k: v for k, v in out.items() if k.lower() in keep}
    return out


def current_traceparent() -> Optional[str]:
    """The W3C traceparent of the span this code runs in, or None when BOSS's tracing is off
    (no OpenTelemetry, or no recording span) — never an invented one."""
    try:
        from opentelemetry import propagate  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — tracing is optional (BOSSAPP-867)
        return None
    carrier: dict[str, str] = {}
    try:
        propagate.inject(carrier)
    except Exception:  # noqa: BLE001
        return None
    value = carrier.get("traceparent")
    return value if isinstance(value, str) and value else None


def call_headers(policy: Mapping, *, run_id: Optional[str] = None, agent: Optional[str] = None,
                 traceparent: Optional[str] = None) -> dict[str, str]:
    """The opt-in per-call metadata for tools/call (empty unless ``forward`` names some)."""
    values = {"run_id": run_id, "agent": agent,
              "traceparent": traceparent if traceparent is not None else (
                  current_traceparent() if "traceparent" in (policy.get("forward") or []) else None)}
    out: dict[str, str] = {}
    for key in policy.get("forward") or []:
        value = str(values.get(key) or "").strip()
        if value and "\n" not in value and "\r" not in value:
            out[FORWARD[key]] = value[:200]
    return out


__all__ = ["FORWARD", "HeadersPolicyError", "apply", "call_headers", "current_traceparent",
           "public", "read", "stored", "validate"]
