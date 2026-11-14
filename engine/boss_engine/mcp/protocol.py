"""MCP REVISIONS THE BOSS SERVER SPEAKS (AP-P5, BOSSAPP-1056; AGENT_PLATFORM §4.5; decision D10:
all three revisions for 12 months).

  2024-11-05  legacy: `initialize` handshake, tools only. Kept byte-for-byte for existing
              clients (a client that asks for it, or names no version, gets exactly it).
  2025-11-25  legacy (also accepted: 2025-06-18, 2025-03-26 — the same tools-only subset):
              negotiated at `initialize`.
  2026-07-28  modern and STATELESS (https://modelcontextprotocol.io/specification/2026-07-28):
              no handshake and no session — every request carries its protocol version and
              client capabilities in `_meta`; `server/discover`; every result has `resultType`;
              list results carry `ttlMs`/`cacheScope`; long-running tools return tasks under the
              `io.modelcontextprotocol/tasks` extension; a person's answer is asked for with an
              `input_required` result (Multi Round-Trip Requests), never a server-initiated
              request. Roots, Sampling and Logging (deprecated) are not offered.

A request is MODERN when its `_meta` names `io.modelcontextprotocol/protocolVersion`; an
`initialize` selects legacy semantics. Pure: no database, no I/O.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Mapping, Optional

MODERN = "2026-07-28"
LEGACY_LATEST = "2025-11-25"
LEGACY_DEFAULT = "2024-11-05"
#: Every revision the server answers, newest first (D10's three plus the two between them).
SUPPORTED = (MODERN, LEGACY_LATEST, "2025-06-18", "2025-03-26", LEGACY_DEFAULT)
LEGACY = SUPPORTED[1:]

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

#: JSON-RPC errors the 2026-07-28 revision defines (the -32020..-32099 MCP range).
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
#: BOSS's own (implementation-defined range -32000..-32019): an in-band answer was refused.
ANSWER_REFUSED = -32010
TASK_NOT_FOUND = -32011

#: How long a tools/list answer may be cached by the client (it is per caller: `private`).
LIST_TTL_MS = 60_000
DISCOVER_TTL_MS = 3_600_000
POLL_INTERVAL_MS = 1_000
#: How long an `input_required` requestState stays good.
REQUEST_STATE_TTL_SECONDS = 15 * 60


def negotiate_initialize(requested: Any) -> str:
    """The legacy revision an `initialize` gets: the one asked for when BOSS speaks it; none
    named → 2024-11-05 (exactly what every existing client got); anything else → the newest
    legacy revision (the spec: answer with another version the server supports)."""
    if requested is None or requested == "":
        return LEGACY_DEFAULT
    return str(requested) if str(requested) in LEGACY else LEGACY_LATEST


def request_meta(params: Any) -> dict:
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    return dict(meta) if isinstance(meta, Mapping) else {}


def modern_version(params: Any) -> Optional[str]:
    """The protocol version a modern (per-request `_meta`) request names, or None (legacy)."""
    value = request_meta(params).get(META_VERSION)
    return str(value) if value else None


def client_capabilities(params: Any) -> dict:
    caps = request_meta(params).get(META_CAPABILITIES)
    return dict(caps) if isinstance(caps, Mapping) else {}


def wants_tasks(params: Any) -> bool:
    ext = client_capabilities(params).get("extensions")
    return isinstance(ext, Mapping) and TASKS_EXTENSION in ext


def elicitation_modes(params: Any) -> set:
    """Which elicitation modes the client declared ({"form"} when it declared elicitation with no
    modes; empty when it declared none — then no elicitation is ever sent to it)."""
    el = client_capabilities(params).get("elicitation")
    if not isinstance(el, Mapping):
        return set()
    modes = {m for m in ("form", "url") if isinstance(el.get(m), Mapping)}
    return modes or {"form"}


def traceparent_of(params: Any) -> Optional[str]:
    """The W3C trace context the caller sent in `_meta` (SEP-414), when it is one."""
    from ..kernel.trace import parse_traceparent

    value = request_meta(params).get("traceparent")
    return str(value).strip().lower() if parse_traceparent(value) else None


def server_capabilities() -> dict:
    return {"tools": {"listChanged": False}, "extensions": {TASKS_EXTENSION: {}}}


def discover_result(server_info: Mapping[str, Any], instructions: str) -> dict:
    return {"resultType": "complete", "supportedVersions": list(SUPPORTED),
            "capabilities": server_capabilities(), "instructions": instructions,
            "_meta": {META_SERVER_INFO: dict(server_info)},
            "ttlMs": DISCOVER_TTL_MS, "cacheScope": "public"}


def complete(result: dict, server_info: Mapping[str, Any]) -> dict:
    """A modern ordinary result: `resultType: complete` and the server's identity."""
    out = dict(result)
    out["resultType"] = "complete"
    meta = dict(out.get("_meta") or {})
    meta[META_SERVER_INFO] = dict(server_info)
    out["_meta"] = meta
    return out


def unsupported_version(requested: Any) -> dict:
    return {"code": UNSUPPORTED_PROTOCOL_VERSION, "message": "Unsupported protocol version",
            "data": {"supported": list(SUPPORTED), "requested": requested}}


# ── requestState: integrity-protected, principal-bound, short-lived (MRTR server reqs 4-5) ──────

_PROCESS_KEY = secrets.token_bytes(32)


def _state_key() -> bytes:
    """BOSS_MCP_STATE_KEY when set (several server instances behind one name); else a key of
    this process — a state minted before a restart is refused and the client is simply asked
    again (requestState is short-lived by design)."""
    configured = os.environ.get("BOSS_MCP_STATE_KEY") or ""
    return hashlib.sha256(configured.encode()).digest() if configured else _PROCESS_KEY


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def request_digest(method: str, name: Any, arguments: Any) -> str:
    raw = json.dumps([method, name, arguments], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def seal_state(payload: Mapping[str, Any], *, principal: Optional[str], digest: str,
               ttl: int = REQUEST_STATE_TTL_SECONDS) -> str:
    body = {**dict(payload), "u": principal or "", "d": digest, "e": int(time.time()) + ttl}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(_state_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(mac)}"


def open_state(value: Any, *, principal: Optional[str], digest: str) -> Optional[dict]:
    """The sealed payload, or None when it fails verification, expired, was minted for another
    caller or for another request (attacker-controlled input: never trusted unverified)."""
    if not isinstance(value, str) or value.count(".") != 1:
        return None
    raw_b64, mac_b64 = value.split(".")
    try:
        raw, mac = _unb64(raw_b64), _unb64(mac_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(mac, hmac.new(_state_key(), raw, hashlib.sha256).digest()):
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if (int(body.get("e") or 0) < int(time.time()) or body.get("u") != (principal or "")
            or body.get("d") != digest):
        return None
    return body


__all__ = [
    "ANSWER_REFUSED", "HEADER_MISMATCH", "LEGACY", "LEGACY_DEFAULT", "LEGACY_LATEST",
    "META_CAPABILITIES", "META_CLIENT_INFO", "META_SERVER_INFO", "META_VERSION", "MODERN",
    "POLL_INTERVAL_MS", "SUPPORTED", "TASKS_EXTENSION", "TASK_NOT_FOUND",
    "UNSUPPORTED_PROTOCOL_VERSION", "client_capabilities", "complete", "discover_result",
    "elicitation_modes", "modern_version", "negotiate_initialize", "open_state",
    "request_digest", "request_meta", "seal_state", "server_capabilities", "traceparent_of",
    "unsupported_version", "wants_tasks",
]
