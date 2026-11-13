"""The Agent Client Protocol message layer BOSS speaks, kept small and versioned (BOSSAPP-853).

ACP (agentclientprotocol.com) is JSON-RPC 2.0 over the agent's stdio, one JSON object per line.
BOSS is the CLIENT; the coding agent (Gemini CLI, OpenCode, ...) is the AGENT. This module is
only the wire: building the client's messages, reading the agent's, and the one mapping BOSS
cares about — an ACP tool call onto the engine contract's canonical tool names. The session
loop lives in acp_code_agent.

PROTOCOL_VERSION is the integer ACP version this layer implements. `initialize` sends it and
refuses an agent that answers with a different one (the spec's rule: the agent answers with the
version it chose; a client that cannot speak it must close the connection).

The subset used (everything else is answered "method not found", which the spec allows):

    client -> agent   initialize, authenticate, session/new, session/load, session/prompt,
                      session/cancel (notification)
    agent -> client   session/update (notification), session/request_permission,
                      fs/read_text_file, fs/write_text_file
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .engine_contract import (
    BASH, CANONICAL_TOOLS, EDIT, GREP, READ, TODO, WEB_FETCH, canonical_tool_input,
    canonical_tool_name,
)

#: The ACP protocol version this layer speaks (ACP's `protocolVersion`, an integer).
PROTOCOL_VERSION = 1
#: This module's own revision: bump MINOR on an additive change, MAJOR with PROTOCOL_VERSION.
LAYER_VERSION = "1.0.0"
CLIENT_INFO = {"name": "boss", "title": "BOSS", "version": LAYER_VERSION}

# JSON-RPC error codes (JSON-RPC 2.0, plus ACP's auth_required).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000

#: ACP `ToolKind` -> BOSS's canonical tool name (engine_contract). `delete` and `move` change
#: the tree, so they are writes BOSS's profile and read-only rule must see as edits.
TOOL_KIND_MAP: dict[str, str] = {
    "read": READ,
    "edit": EDIT,
    "delete": EDIT,
    "move": EDIT,
    "search": GREP,
    "execute": BASH,
    "fetch": WEB_FETCH,
    "think": TODO,
}

#: ACP `StopReason` -> the engine contract's stop_reason.
STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "done",
    "max_tokens": "turn_cap",
    "max_turn_requests": "turn_cap",
    "refusal": "error",
    "cancelled": "error",
}


class AcpProtocolError(RuntimeError):
    """The agent broke the protocol, or answered a request with an error."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


# ── client -> agent ──────────────────────────────────────────────────────────────────────────

def request(rid: int, method: str, params: Optional[Mapping[str, Any]] = None) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params or {})}


def notification(method: str, params: Optional[Mapping[str, Any]] = None) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}


def result(rid: Any, value: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": value}


def error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def initialize_params(*, read_only: bool) -> dict:
    """BOSS offers file reads always and file writes only to a writing session; never a
    terminal (the agent runs its own commands, inside BOSS's sandbox)."""
    return {"protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": not read_only},
                                   "terminal": False},
            "clientInfo": CLIENT_INFO}


def new_session_params(cwd: str, mcp_servers: list) -> dict:
    return {"cwd": cwd, "mcpServers": list(mcp_servers)}


def load_session_params(session_id: str, cwd: str, mcp_servers: list) -> dict:
    return {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers)}


def prompt_params(session_id: str, text: str) -> dict:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


def encode(message: Mapping[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":")) + "\n"


def decode(line: str) -> Optional[dict]:
    """One line from the agent, or None for a blank/non-JSON line (agents log to stdout)."""
    line = line.strip()
    if not line or line[0] != "{":
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("jsonrpc") == "2.0" else None


# ── what the agent's initialize answer says ──────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentInfo:
    protocol_version: int
    load_session: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False
    auth_methods: tuple[str, ...] = ()
    name: str = ""

    @classmethod
    def from_result(cls, value: Mapping[str, Any]) -> "AgentInfo":
        caps = value.get("agentCapabilities") or {}
        mcp = caps.get("mcpCapabilities") or caps.get("mcp") or {}
        info = value.get("agentInfo") or {}
        return cls(protocol_version=int(value.get("protocolVersion") or 0),
                   load_session=bool(caps.get("loadSession")),
                   mcp_http=bool(mcp.get("http")), mcp_sse=bool(mcp.get("sse")),
                   auth_methods=tuple(str(m.get("id")) for m in value.get("authMethods") or []
                                      if isinstance(m, Mapping) and m.get("id")),
                   name=str(info.get("name") or ""))


# ── agent -> client: tool calls, onto the engine contract ────────────────────────────────────

def canonical_tool(tool_call: Mapping[str, Any]) -> str:
    """BOSS's name for an ACP tool call: its `kind` first (the protocol's own classification),
    then its title/name through the engine contract's aliases; `other` or nothing known keeps
    the agent's own words, so an MCP tool still shows as itself."""
    kind = str(tool_call.get("kind") or "").strip().lower()
    if kind in TOOL_KIND_MAP:
        return TOOL_KIND_MAP[kind]
    title = str(tool_call.get("title") or tool_call.get("name") or "").strip()
    first = title.split(" ", 1)[0] if title else ""
    for candidate in (title, first):
        named = canonical_tool_name(candidate)
        if named in CANONICAL_TOOLS:
            return named
    return title or "other"


def tool_input(tool_call: Mapping[str, Any]) -> dict:
    """The call's arguments in the profile's keys: rawInput, plus the first location's path."""
    raw = tool_call.get("rawInput")
    args: dict = dict(raw) if isinstance(raw, Mapping) else ({"input": raw} if raw else {})
    locations = tool_call.get("locations") or []
    if locations and isinstance(locations[0], Mapping) and locations[0].get("path"):
        args.setdefault("path", str(locations[0]["path"]))
    if tool_call.get("kind") in ("delete", "move"):
        args.setdefault("acp_kind", tool_call.get("kind"))
    name = canonical_tool(tool_call)
    out = canonical_tool_input(name, args)
    return dict(out) if isinstance(out, Mapping) else args


@dataclass
class ToolCallState:
    tool_call_id: str
    name: str
    title: str = ""
    args: dict = field(default_factory=dict)
    status: str = "pending"


def text_of(content: Any) -> str:
    """The text in an ACP ContentBlock (or a list of them)."""
    if isinstance(content, list):
        return "".join(text_of(c) for c in content)
    if isinstance(content, Mapping):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        if "content" in content:
            return text_of(content.get("content"))
    return ""


# ── permissions ──────────────────────────────────────────────────────────────────────────────

def pick_option(options: list, allow: bool) -> Optional[str]:
    """The optionId BOSS selects: a ONE-TIME allow or reject (never `*_always` — BOSS decides
    each call), falling back to the always variant only when the agent offers nothing else."""
    want = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in want:
        for option in options or []:
            if isinstance(option, Mapping) and option.get("kind") == kind:
                return str(option.get("optionId"))
    return None


def permission_outcome(option_id: Optional[str]) -> dict:
    """The `session/request_permission` result. No matching option is a cancellation, which
    every agent must treat as "not allowed"."""
    if option_id is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}
