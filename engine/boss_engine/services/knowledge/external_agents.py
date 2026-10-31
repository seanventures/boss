"""KG P6a: registration of a bank's own HTTP-hosted agent (engine, no console — see P6b).

A bank's agent runs one workflow step under BOSS's contract guarantee. Before it can be named
on a step's captain, it is registered here: a display name, an HTTPS endpoint, a reference to
the secret backing its auth header, a human sponsor, and a status. Registration grants it no
reach by itself — the per-execution token (``external_agent_tokens``) is what the KG MCP server
actually enforces, scoped fresh from the step's contract every time.
"""
from __future__ import annotations

import ipaddress
import socket
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection


class ExternalAgentError(Exception):
    """An external agent's registration or endpoint was refused. The message is the plain reason."""


#: Tailnet addressing (MagicDNS), the one exception beside loopback a dev install may use
#: without HTTPS (tech plan §"Resolved ambiguities").
_TAILNET_SUFFIX = ".ts.net"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def validate_endpoint(url: str) -> None:
    """HTTPS is required, except plain HTTP to a loopback or tailnet host on a dev install.
    Credentials embedded in the URL (``user:pass@host``) are refused outright: they would sit in
    the clear in `external_agents.endpoint_url`, in logs, and in every trace of this call — the
    secret belongs only behind `secret_ref`, resolved fresh per call."""
    parsed = urlparse((url or "").strip())
    if parsed.username or parsed.password:
        raise ExternalAgentError(plain_reason(
            "endpoint_rejected", expected="no embedded credentials in the URL"))
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname:
        host = parsed.hostname.lower()
        if host in _LOOPBACK_HOSTS or host.endswith(_TAILNET_SUFFIX):
            return
    raise ExternalAgentError(plain_reason("endpoint_rejected", expected="https, or http to localhost/a tailnet host"))


def resolve_validated_address(url: str) -> Optional[str]:
    """Resolves the endpoint's host exactly once and validates every address it returns, exactly
    as :func:`check_endpoint_egress` always has — but, unlike that function, hands the specific
    validated IP back so a caller can pin its actual outbound connection to it.

    Without this, the validated DNS lookup and the real connection a moment later are two
    independent resolutions of the same hostname: an attacker who controls DNS for it (a
    short-TTL "rebinding" record) can answer the first lookup with a public address and the
    second, at connect time, with a private/internal one (e.g. link-local metadata), landing
    the real request somewhere the egress check never saw. Pinning to the address actually
    validated here closes that gap. Returns ``None`` for the loopback/tailnet dev-install
    carve-out, where the hostname is trusted directly and there is nothing to pin."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        raise ExternalAgentError(plain_reason("endpoint_rejected", expected="a resolvable host"))
    if host in _LOOPBACK_HOSTS or host.endswith(_TAILNET_SUFFIX):
        return None
    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ExternalAgentError(plain_reason("endpoint_rejected", expected="a resolvable host")) from None
    ips: list[str] = []
    for info in resolved:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise ExternalAgentError(plain_reason("endpoint_rejected", expected="a public host")) from None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ExternalAgentError(plain_reason("endpoint_rejected", expected="a public, non-private endpoint"))
        ips.append(str(ip))
    if not ips:
        raise ExternalAgentError(plain_reason("endpoint_rejected", expected="a resolvable host"))
    return ips[0]


def check_endpoint_egress(url: str) -> None:
    """Refuses an endpoint whose resolved address is private/loopback/link-local/reserved,
    unless its hostname is one of the already-allowed loopback/tailnet exceptions
    (:func:`validate_endpoint`'s dev-install carve-out). Called at every call, not only at
    registration, so a host that re-resolves to a private address after registration is still
    refused — a hostname with mixed public/private A records is refused as a whole, never
    partially trusted.

    Callers that go on to make the real outbound call should prefer
    :func:`resolve_validated_address` and pin their connection to the address it returns,
    rather than re-resolving the hostname a second time (see that function's docstring)."""
    resolve_validated_address(url)


def plain_reason(kind: str, *, expected: str = "", actual: str = "") -> str:
    """The one refusal sentence this feature raises everywhere — scope denial, an unregistered
    or cross-project agent, a dead endpoint, a bad response, a missing write. Unregistered and
    cross-project share identical wording so neither tells an outsider which case it is."""
    sentences = {
        "unregistered_agent": "this step names an external agent this project does not hold.",
        "endpoint_rejected": f"the external agent's endpoint is not allowed ({expected}).",
        "no_contract": "an external_agent step must declare a contract.",
        "bad_captain": "an external_agent step's captain must be {external_agent: <id>}.",
        "scope_denied": f"this step may read {expected}; not {actual}.".strip(),
        "token_refused": "this token is not valid for this call.",
        "non_2xx": "the external agent's endpoint returned an error.",
        "timeout": "the external agent's endpoint did not answer in time.",
        "unreachable": "could not reach the external agent.",
        "invalid_json": "the external agent's endpoint did not return valid JSON.",
        "oversized_body": "the external agent's endpoint returned a response too large to accept.",
        "kg_mcp_not_configured": "no Knowledge-Graph MCP server is configured for this step.",
        "secret_unavailable": "the external agent's auth secret could not be resolved.",
    }
    return sentences.get(kind, kind)


def scrub(text_value: str, *secrets: str) -> str:
    """Every given secret value, redacted from ``text_value`` — the one function the outbound
    HTTP client, the retry path and the trace writer all call before a response body, header or
    error ever reaches a message, a log line or a trace row."""
    out = text_value or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def register(connection: Connection, project_id: str, *, display_name: str, endpoint_url: str,
             secret_ref: str, sponsor_principal_id: str, created_by: str) -> dict[str, Any]:
    validate_endpoint(endpoint_url)
    agent_id = f"ext_{uuid.uuid4().hex[:16]}"
    connection.execute(text(
        "INSERT INTO external_agents "
        "(id, project_id, display_name, endpoint_url, secret_ref, sponsor_principal_id, created_by) "
        "VALUES (:id, :p, :name, :url, :sref, :sponsor, :by)"
    ), {"id": agent_id, "p": project_id, "name": display_name, "url": endpoint_url,
        "sref": secret_ref, "sponsor": sponsor_principal_id, "by": created_by})
    return get(connection, project_id, agent_id) or {}


def get(connection: Connection, project_id: str, agent_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, project_id, display_name, endpoint_url, secret_ref, sponsor_principal_id, "
        "status, created_by, created_at, updated_at FROM external_agents "
        "WHERE id = :id AND project_id = :p"
    ), {"id": agent_id, "p": project_id}).mappings().first()
    return dict(row) if row else None


def active_for_project(connection: Connection, project_id: str, agent_id: str) -> bool:
    """Whether ``agent_id`` is registered to THIS project and active — the fact spec validation
    checks. An id that exists but belongs to another project reads exactly like one that does
    not exist at all (Risk #4): callers must use :func:`plain_reason`'s shared wording, never
    surface which case this was."""
    agent = get(connection, project_id, agent_id)
    return bool(agent) and agent["status"] == "active"


def set_status(connection: Connection, project_id: str, agent_id: str, status: str, *,
               actor_id: str, reason: str) -> dict[str, Any]:
    if status not in ("active", "suspended"):
        raise ValueError("status must be 'active' or 'suspended'")
    connection.execute(text(
        "UPDATE external_agents SET status = :s, updated_at = now() "
        "WHERE id = :id AND project_id = :p"
    ), {"s": status, "id": agent_id, "p": project_id})
    return get(connection, project_id, agent_id) or {}
