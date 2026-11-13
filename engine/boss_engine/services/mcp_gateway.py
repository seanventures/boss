"""THE MCP GATEWAY: A BUILD AGENT CAN ASK, WAIT FOR APPROVAL AND REPORT MID-RUN (BOSSAPP-814).

WHY THIS EXISTS
---------------
code_task_mcp wrote an `mcp.json` pointing the coding harness STRAIGHT at the MCP servers its
agent holds. BOSS saw none of those calls: no policy decision, no receipt, no audit row. And
because "a harness running unattended cannot pause for an approval mid-run", every Ask tool was
passed as `--disallowedTools` — on the main build path Ask meant Off. The coder had no way to ask
a person anything, say it was blocked, or propose a follow-up.

WHAT IT IS
----------
The same pattern as the model proxy (model_proxy.py): the process that starts an agent session
runs a small server on loopback, and the session gets, in place of the real servers,

  * one URL per held server — `http://127.0.0.1:<port>/v1/mcp-gateway/<run token>/<server>` —
  * and one more, `.../<run token>/boss_run`, carrying the run's own self-service tools.

The run token (`boss_mcpgw_<64 hex>`) is bound to the project, the agent and (once the session
starts) the run; it is registered with the log redactor, dies when the run's MCP file is cleaned
up, and is refused anywhere else. The file no longer carries any upstream sign-in: the gateway
resolves it at call time (mcp_registry.auth_headers), so the harness never holds it.

The gateway is an MCP server (Streamable HTTP, JSON-RPC 2.0: initialize, tools/list,
tools/call, ping, notifications) that PROXIES to the real servers:

  * tools/list  — the upstream server's own list, read live (the registry's last list if it
    cannot be reached);
  * tools/call  — `tool_policy_verdict` decides allow | ask | deny; an allowed call goes through
    mcp_registry.call_tool_with_receipt (receipt, dedupe, audit row, the review/Off/Ask
    backstops); a denied one is audited and answered with a typed error; an ASK call PARKS:
    a decisions row is raised (the console's Approvals queue answers it as any other), the MCP
    call is held open — answered as an SSE stream that carries keepalive comments (and MCP
    progress notifications when the client asked for them) — and the run's stage clock stops
    (stage_clock.parked). Approved: the call runs, carrying that decision as its approval.
    Denied, withdrawn or timed out: a typed error the agent can read and act on.

THE SELF-SERVICE TOOLS (server `boss_run`)
------------------------------------------
ask_person(question, options?)       parks on a decision; returns the person's answer
request_approval(action, args)       parks on a decision; approved -> ok, denied -> typed error
report_blocked(reason)               a run event (code_task.agent_blocked) + a live-feed line
report_progress(note)                a run event (code_task.agent_progress) + a live-feed line
propose_followup(title, body)        a run event (code_task.followup_proposed) + a live-feed line

THE SWITCH
----------
BOSS_MCP_GATEWAY=on|off, OFF by default: off, code_task_mcp writes exactly the file it always did
(test_mcp_gateway_off_is_byte_identical). On, every harness's MCP traffic flows through here.

WHAT IS NOT COVERED
-------------------
A micro-VM guest or a remote sandbox has no loopback shared with BOSS (as with the model proxy).
`tool_policy_verdict` asks the ordered policy engine (BOSSAPP-815, tool_policy_engine.decide).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Mapping, Optional

from sqlalchemy import text

from . import stage_clock

log = logging.getLogger(__name__)

FLAG = "BOSS_MCP_GATEWAY"
ASK_TIMEOUT_ENV = "BOSS_MCP_GATEWAY_ASK_TIMEOUT_SECONDS"
KEEPALIVE_ENV = "BOSS_MCP_GATEWAY_KEEPALIVE_SECONDS"
#: How long a parked call waits for a person before it is answered "timed out" (30 minutes).
DEFAULT_ASK_TIMEOUT_SECONDS = 1800
#: How often a parked call writes a keepalive on its open stream.
DEFAULT_KEEPALIVE_SECONDS = 15
TOKEN_PREFIX = "boss_mcpgw_"
PATH_PREFIX = "/v1/mcp-gateway/"
HOST = "127.0.0.1"
#: The run's own self-service tools are served under this server name.
RUN_SERVER = "boss_run"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 8 * 1024 * 1024
MIN_TTL_SECONDS = 3600
TTL_GRACE_SECONDS = 900

#: The decision actions this module raises (decision_actions.RECORDED_ON_APPROVAL: approving
#: records the answer; the parked call reads it — nothing else is started by the card).
ACTION_TOOL_CALL = "gateway_tool_call"
ACTION_ASK_PERSON = "gateway_ask_person"
ACTION_REQUEST_APPROVAL = "gateway_request_approval"

#: The typed errors an agent receives (structuredContent.error.type = "boss.<kind>").
ERROR_KINDS = ("denied_by_policy", "denied_by_person", "approval_timed_out", "run_ended",
               "tool_failed", "unknown_tool", "bad_arguments")


def enabled() -> bool:
    """BOSS_MCP_GATEWAY: off unless set to a true word."""
    return (os.environ.get(FLAG) or "").strip().lower() in ("1", "true", "yes", "on")


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


def ask_timeout_seconds() -> float:
    return _env_seconds(ASK_TIMEOUT_ENV, DEFAULT_ASK_TIMEOUT_SECONDS)


def keepalive_seconds() -> float:
    return _env_seconds(KEEPALIVE_ENV, DEFAULT_KEEPALIVE_SECONDS)


# ── The policy seam ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    decision: str            # allow | ask | deny
    reason: str              # why, as a code: allow, ask, unreviewed, off, not_granted,
    #                          policy (a rule asked or denied), rate_limited
    #: What tool_policy_engine.decide matched and why (BOSSAPP-815). Not part of equality, so a
    #: verdict still reads as its decision and reason.
    matched_policy_ids: tuple = field(default=(), compare=False)
    explanation: str = field(default="", compare=False)
    rule_id: Optional[str] = field(default=None, compare=False)


def tool_policy_verdict(server: Mapping[str, Any], tool_name: str, *,
                        granted_tools: Optional[list] = None,
                        arguments: Optional[Mapping[str, Any]] = None,
                        project_id: Optional[str] = None,
                        run_id: Optional[str] = None,
                        role_key: Optional[str] = None,
                        connection: Any = None,
                        record: bool = False) -> Verdict:
    """THE ONE PLACE a proxied call's fate is decided: the agent's grant, then the ordered
    tool-policy engine (BOSSAPP-815, :func:`tool_policy_engine.decide`) — the same decision the
    receipt door and the kernel make, so the three cannot disagree.

    deny  — the agent's grant does not name the tool, the operator turned it Off, a deny rule
            matched, or a rate limit is spent;
    ask   — the server is unreviewed, the tool is set to Ask (every tier >= 2 tool's floor), it
            is quarantined, or an ask rule matched;
    allow — everything else (including a tier 0-1 Ask tool an "Always allow" lets run).

    ``connection`` reads the project's rules; without one only the registry's own Allow / Ask /
    Off and the ceilings apply. ``record`` counts the deciding rule's hit.
    """
    from .tool_policy_engine import context_for, decide

    granted = {str(t) for t in (granted_tools or [])}
    if granted and tool_name not in granted:
        return Verdict("deny", "not_granted", ("guard:not_granted",),
                       f"DENY: {tool_name} is not in this agent's grant.")
    found = decide(context_for(dict(server), tool_name, arguments,
                               project_id=project_id or server.get("project_id"),
                               run_id=run_id, role_key=role_key),
                   connection=connection, record=record)
    return Verdict(found["decision"], str(found["reason"]),
                   tuple(found["matched_policy_ids"]), str(found["explanation"]),
                   found.get("rule_id"))


# ── Results ──────────────────────────────────────────────────────────────────────────────────

def tool_error(kind: str, message: str, **extra: Any) -> dict:
    """An MCP CallToolResult the agent reads as a failure it can act on: the text says what
    happened and what to do; structuredContent.error.type is `boss.<kind>`."""
    return {"content": [{"type": "text", "text": f"[boss.{kind}] {message}"}],
            "isError": True,
            "structuredContent": {"error": {"type": f"boss.{kind}", "message": message,
                                            **{k: v for k, v in extra.items() if v is not None}}}}


def tool_text(value: Any, *, meta: Optional[dict] = None) -> dict:
    if isinstance(value, dict) and set(value) == {"text"} and isinstance(value["text"], str):
        body = value["text"]
    elif isinstance(value, str):
        body = value
    else:
        body = json.dumps(value, default=str)
    out: dict = {"content": [{"type": "text", "text": body}], "isError": False}
    if isinstance(value, dict) and not (set(value) == {"text"}):
        out["structuredContent"] = value
    if meta:
        out["_meta"] = meta
    return out


def _rpc_result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ── Self-service tools ───────────────────────────────────────────────────────────────────────

RUN_TOOLS: list[dict] = [
    {"name": "ask_person",
     "description": ("Ask a person a question and wait for the answer (the build pauses, its "
                     "clock stopped, until they reply in BOSS's Approvals). Use it when you "
                     "cannot decide something yourself."),
     "inputSchema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"},
                     "description": "Answers to offer, if the question has a few."}}}},
    {"name": "request_approval",
     "description": ("Ask a person to approve an action before you take it, and wait. Denied "
                     "comes back as an error of type boss.denied_by_person."),
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "description": "What you want to do, in one sentence."},
         "args": {"type": "object", "description": "The details the person should see."}}}},
    {"name": "report_blocked",
     "description": "Say you are blocked and why. Shown on the run page; does not wait.",
     "inputSchema": {"type": "object", "required": ["reason"],
                     "properties": {"reason": {"type": "string"}}}},
    {"name": "report_progress",
     "description": "Record a short progress note on the run page. Does not wait.",
     "inputSchema": {"type": "object", "required": ["note"],
                     "properties": {"note": {"type": "string"}}}},
    {"name": "propose_followup",
     "description": ("Propose follow-up work you noticed but should not do in this run. "
                     "Recorded on the run page for a person to pick up. Does not wait."),
     "inputSchema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string"}, "body": {"type": "string"}}}},
]
_RUN_TOOL_NAMES = frozenset(t["name"] for t in RUN_TOOLS)
#: BOSSAPP-859: listed only to an agent that holds a signing key (agent_keys). The private key
#: never leaves BOSS: the agent sends what to sign and gets a detached signature back.
SIGN_TOOL: dict = {
    "name": "sign",
    "description": ("Sign something you produced with your own agent key, so anyone can check "
                    "it came from you using your public keys (the returned jwks path). Returns "
                    "a detached Ed25519 signature; your key never leaves BOSS."),
    "inputSchema": {"type": "object", "required": ["payload"], "properties": {
        "payload": {"type": "string", "description": "The exact text to sign."},
        "purpose": {"type": "string", "enum": ["output"],
                    "description": "What the signature is for (always 'output' here)."}}}}
#: report_* tools: (event type, the argument carried, its label on the live feed).
_REPORTS = {
    "report_blocked": ("code_task.agent_blocked", "reason", "Blocked"),
    "report_progress": ("code_task.agent_progress", "note", "Progress"),
    "propose_followup": ("code_task.followup_proposed", "title", "Follow-up proposed"),
}


# ── Grants ───────────────────────────────────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class GatewayGrant:
    """What one run token may reach. The token itself is never stored, only its hash."""

    token_hash: str
    project_id: str
    expires_at: float
    #: cli server name -> {"server_id": ..., "granted_tools": [...]}
    servers: dict = field(default_factory=dict)
    role_key: Optional[str] = None
    run_id: Optional[str] = None
    revoked: bool = False
    gateway_id: str = ""
    calls: dict = field(default_factory=lambda: {"allow": 0, "ask": 0, "deny": 0, "run": 0})

    def live(self, now: Optional[float] = None) -> bool:
        return not self.revoked and (now or time.time()) < self.expires_at

    @property
    def run_key(self) -> str:
        """What the calls are bound to: the run, or this grant when no run was bound."""
        return self.run_id or self.gateway_id


class Sink:
    """Where a parked call says it is still alive. The HTTP handler writes SSE comments; a
    direct caller (a test) passes nothing."""

    def keepalive(self) -> None:  # pragma: no cover — interface
        return None

    def progress(self, message: str) -> None:  # pragma: no cover — interface
        return None


class ClientGone(RuntimeError):
    """The harness hung up while its call was parked."""


class McpGateway:
    """The loopback gateway and its grants. One per process (`get`); tests build their own."""

    def __init__(self, *, db: Optional[Callable[[], Any]] = None, settings: Any = None,
                 host: str = HOST, poll_interval: float = 0.5,
                 ask_timeout: Optional[float] = None, keepalive: Optional[float] = None) -> None:
        self._db = db
        self._settings = settings
        self.host = host
        self.poll_interval = poll_interval
        self._ask_timeout = ask_timeout
        self._keepalive = keepalive
        self._grants: dict[str, GatewayGrant] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    # ---- wiring ------------------------------------------------------------------------------
    @property
    def ask_timeout(self) -> float:
        return float(self._ask_timeout) if self._ask_timeout else ask_timeout_seconds()

    @property
    def keepalive_every(self) -> float:
        return float(self._keepalive) if self._keepalive else keepalive_seconds()

    def engine(self) -> Any:
        if self._db is not None:
            return self._db()
        from ..db import get_engine

        return get_engine()

    def settings(self) -> Any:
        if self._settings is not None:
            return self._settings
        from ..config import get_settings

        return get_settings()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[Any]:
        with self.engine().begin() as connection:
            yield connection

    # ---- the server --------------------------------------------------------------------------
    @property
    def port(self) -> int:
        self.start()
        assert self._server is not None
        return int(self._server.server_address[1])

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            server = ThreadingHTTPServer((self.host, 0), _handler_for(self))
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, args=(0.5,), daemon=True,
                             name="boss-mcp-gateway").start()
            self._server = server

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def base_url(self, token: str) -> str:
        return f"http://{self.host}:{self.port}{PATH_PREFIX}{token}"

    # ---- grants ------------------------------------------------------------------------------
    def issue(self, project_id: str, servers: Mapping[str, Mapping[str, Any]], *,
              role_key: Optional[str] = None, run_id: Optional[str] = None,
              ttl: float = MIN_TTL_SECONDS + TTL_GRACE_SECONDS) -> tuple[str, str]:
        """A run token for these servers; returns (token, base URL)."""
        token = TOKEN_PREFIX + secrets.token_hex(32)
        _register_redaction(token)
        grant = GatewayGrant(token_hash=_hash(token), project_id=project_id,
                             expires_at=time.time() + max(1.0, float(ttl)),
                             servers={str(k): dict(v) for k, v in servers.items()},
                             role_key=role_key, run_id=run_id,
                             gateway_id=f"mcpgw_{_hash(token)[:16]}")
        with self._lock:
            self._sweep()
            self._grants[grant.token_hash] = grant
        return token, self.base_url(token)

    def grant_for(self, token: str) -> Optional[GatewayGrant]:
        with self._lock:
            return self._grants.get(_hash(token))

    def bind_run(self, token: str, run_id: Optional[str]) -> None:
        """Bind the token to the run whose session uses it (the first one wins)."""
        if not run_id:
            return
        with self._lock:
            grant = self._grants.get(_hash(token))
            if grant is not None and not grant.run_id:
                grant.run_id = str(run_id)

    def revoke(self, token: str) -> None:
        with self._lock:
            grant = self._grants.get(_hash(token))
            if grant is not None:
                grant.revoked = True
        if grant is not None and grant.run_id:
            stage_clock.forget(grant.run_id)

    def revoke_run(self, run_id: str) -> int:
        n = 0
        with self._lock:
            for grant in self._grants.values():
                if grant.run_id == run_id and not grant.revoked:
                    grant.revoked = True
                    n += 1
        stage_clock.forget(run_id)
        return n

    def _sweep(self) -> None:
        cutoff = time.time() - 6 * 3600
        for key in [k for k, g in self._grants.items()
                    if g.expires_at < cutoff or (g.revoked and g.expires_at < time.time())]:
            del self._grants[key]

    def authorise(self, token: str) -> tuple[int, str, Optional[GatewayGrant]]:
        if not token.startswith(TOKEN_PREFIX):
            return 401, "no_run_token", None
        with self._lock:
            grant = self._grants.get(_hash(token))
        if grant is None:
            return 401, "unknown_token", None
        if grant.revoked:
            return 401, "token_revoked", None
        if not grant.live():
            return 401, "token_expired", None
        return 200, "", grant

    # ---- JSON-RPC ----------------------------------------------------------------------------
    def handle(self, grant: GatewayGrant, server_key: str, message: Mapping[str, Any],
               sink: Optional[Sink] = None) -> Optional[dict]:
        """Answer one JSON-RPC message for `server_key`. None for a notification."""
        rid = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" not in message:
            return None                       # notifications/initialized, cancelled, ...
        if server_key != RUN_SERVER and server_key not in grant.servers:
            return _rpc_error(rid, -32004, f"no server '{server_key}' on this run")
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            return _rpc_result(rid, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": f"boss-mcp-gateway/{server_key}", "version": "1.0.0"},
                "instructions": ("Tool calls here go through BOSS: some wait for a person's "
                                 "approval before they run.")})
        if method == "ping":
            return _rpc_result(rid, {})
        if method == "tools/list":
            tools = (RUN_TOOLS + self._keyed_tools(grant) if server_key == RUN_SERVER
                     else self._upstream_tools(grant, server_key))
            return _rpc_result(rid, {"tools": tools})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _rpc_result(rid, tool_error("bad_arguments",
                                                   "arguments must be an object"))
            sink = _with_progress(sink, params)
            if server_key == RUN_SERVER:
                return _rpc_result(rid, self.call_run_tool(grant, name, arguments, sink))
            return _rpc_result(rid, self.call_upstream(grant, server_key, name, arguments, sink))
        if method in ("resources/list", "prompts/list"):
            return _rpc_result(rid, {method.split("/")[0]: []})
        return _rpc_error(rid, -32601, f"method not found: {method}")

    # ---- upstream ----------------------------------------------------------------------------
    def _server_row(self, connection: Any, grant: GatewayGrant, server_key: str
                    ) -> Optional[dict]:
        from .mcp_registry import get_mcp_server

        meta = grant.servers.get(server_key) or {}
        return get_mcp_server(connection, grant.project_id, str(meta.get("server_id") or ""))

    def _upstream_tools(self, grant: GatewayGrant, server_key: str) -> list[dict]:
        from .mcp_client import MCPClient
        from .mcp_registry import auth_headers

        with self._tx() as c:
            server = self._server_row(c, grant, server_key)
            if server is None:
                return []
            try:
                headers = auth_headers(c, self.settings(), grant.project_id, server)
            except Exception:  # noqa: BLE001 — listed from the registry instead
                headers = None
        tools: list[dict] = []
        if headers is not None:
            try:
                tools = MCPClient(self.settings()).list_tools(server["url"], headers=headers)
            except Exception as exc:  # noqa: BLE001 — the registry's last list stands in
                log.info("mcp gateway: live tools/list failed for %s: %s", server_key,
                         type(exc).__name__)
        if not tools:
            tools = [dict(t) for t in server.get("tools") or [] if isinstance(t, dict)]
        out = []
        for tool in tools:
            if not tool.get("name"):
                continue
            item = dict(tool)
            item.setdefault("inputSchema", {"type": "object"})
            out.append(item)
        return out

    def call_upstream(self, grant: GatewayGrant, server_key: str, tool: str,
                      arguments: dict, sink: Optional[Sink] = None) -> dict:
        from .tool_call_audit import record_tool_call

        meta = grant.servers.get(server_key) or {}
        with self._tx() as c:
            server = self._server_row(c, grant, server_key)
            if server is None:
                return tool_error("unknown_tool", f"the server '{server_key}' is no longer "
                                  "registered in this project")
            # A deny ends the call here, so the rule's hit is counted here; an allowed or
            # asked call is counted once, at the receipt door it then goes through.
            verdict = tool_policy_verdict(server, tool, granted_tools=meta.get("granted_tools"),
                                          arguments=arguments, project_id=grant.project_id,
                                          run_id=grant.run_key, role_key=grant.role_key,
                                          connection=c)
            if verdict.decision == "deny":
                if verdict.rule_id:
                    tool_policy_verdict(server, tool, arguments=arguments,
                                        project_id=grant.project_id, run_id=grant.run_key,
                                        role_key=grant.role_key, connection=c, record=True)
                grant.calls["deny"] += 1
                record_tool_call(c, project_id=grant.project_id, run_id=grant.run_key,
                                 agent="mcp_gateway", principal=grant.role_key,
                                 server_id=server.get("id"), tool=tool, arguments=arguments,
                                 decision="deny",
                                 matched_policy_ids=list(verdict.matched_policy_ids)
                                 or [verdict.reason],
                                 decision_id=None, outcome="refused", latency_ms=0,
                                 screen=({"policy": {"decision": "deny",
                                                     "rule_id": verdict.rule_id,
                                                     "explanation": verdict.explanation}}
                                         if verdict.rule_id else None))
                why = ("it is turned off in Skills & MCP" if verdict.reason == "off"
                       else "it is not in this agent's grant" if verdict.reason == "not_granted"
                       else verdict.explanation.rstrip("."))
                return tool_error(
                    "denied_by_policy",
                    (f"{tool} on '{server.get('name')}' is not allowed for this run: {why}. "
                     "Do the work another way, or call report_blocked if you cannot."),
                    reason=verdict.reason, tool=tool, server=server.get("name"))
        decision_id = None
        if verdict.decision == "ask":
            grant.calls["ask"] += 1
            decision_id, answer = self._ask(grant, sink, kind=ACTION_TOOL_CALL, server=server,
                                            tool=tool, arguments=arguments,
                                            reason=verdict.reason)
            if answer["status"] != "approved":
                with self._tx() as c:
                    record_tool_call(c, project_id=grant.project_id, run_id=grant.run_key,
                                     agent="mcp_gateway", principal=grant.role_key,
                                     server_id=server.get("id"), tool=tool, arguments=arguments,
                                     decision="ask",
                                     matched_policy_ids=list(verdict.matched_policy_ids)
                                     or [verdict.reason],
                                     decision_id=decision_id, outcome="refused", latency_ms=0)
                return _not_released(answer, decision_id, what=f"{tool} on "
                                     f"'{server.get('name')}'")
        else:
            grant.calls["allow"] += 1
        return self._execute(grant, server_key, tool, arguments, decision_id)

    def _execute(self, grant: GatewayGrant, server_key: str, tool: str, arguments: dict,
                 decision_id: Optional[str]) -> dict:
        """The call itself, through the one effect every road to an MCP tool shares."""
        from .mcp_registry import McpServerError, call_tool_with_receipt

        call_ref = secrets.token_hex(8)
        with self._tx() as c:
            server = self._server_row(c, grant, server_key)
            if server is None:
                return tool_error("unknown_tool", f"the server '{server_key}' is gone")
            try:
                called = call_tool_with_receipt(
                    c, self.settings(), grant.project_id, server, tool, arguments,
                    dedupe_key=f"mcpgw:{grant.run_key}:{server['id']}:{tool}:{call_ref}",
                    tenant_scope=_tenant_scope(c, grant.project_id),
                    approved_by=decision_id,
                    approval_about={"run_id": grant.run_key},
                    # Who is calling, so an agent-scoped rule decides here as it did above.
                    policy_context={"role_key": grant.role_key, "run_id": grant.run_key},
                    agent=grant.role_key or "mcp_gateway")
            except McpServerError as refused:
                # The registry's own backstop (review, Off, Ask, changed arguments) said no.
                return tool_error("denied_by_policy", str(refused), tool=tool,
                                  decision_id=decision_id)
            except Exception as exc:  # noqa: BLE001 — the agent reads it; the receipt kept it
                return tool_error("tool_failed", f"{tool} failed: {str(exc)[:1500]}",
                                  tool=tool, error=type(exc).__name__)
        meta = {"boss/receipt_id": called.get("receipt_id")}
        if called.get("untrusted"):
            meta["boss/untrusted"] = True
            meta["boss/screen"] = called.get("screen")
        if decision_id:
            meta["boss/decision_id"] = decision_id
        return tool_text(called.get("output"), meta=meta)

    # ---- the run's own tools -----------------------------------------------------------------
    def _keyed_tools(self, grant: GatewayGrant) -> list[dict]:
        """[SIGN_TOOL] when the run's agent holds a signing key (BOSSAPP-859), else []."""
        if not grant.role_key:
            return []
        try:
            from .agent_keys import has_active_key

            with self._tx() as c:
                return [SIGN_TOOL] if has_active_key(c, grant.project_id, grant.role_key) else []
        except Exception:  # noqa: BLE001 — a lookup never takes the run's tools away
            log.debug("mcp gateway: signing-key lookup failed", exc_info=True)
            return []

    def _sign(self, grant: GatewayGrant, arguments: dict) -> dict:
        from .agent_keys import AgentKeyError, sign

        payload = arguments.get("payload")
        if not isinstance(payload, str) or not payload:
            return tool_error("bad_arguments", "sign needs 'payload' (text)")
        if str(arguments.get("purpose") or "output") != "output":
            return tool_error("bad_arguments", "sign's purpose is 'output'")
        if not grant.role_key:
            return tool_error("no_agent", "this run is not an agent's, so there is no key")
        try:
            with self._tx() as c:
                signed = sign(c, grant.project_id, grant.role_key, payload, purpose="output")
        except AgentKeyError as error:
            return tool_error("no_key", str(error))
        self._run_event(grant, "agent.output_signed",
                        {"kid": signed["kid"], "agent_id": signed["agent_id"],
                         "role_key": grant.role_key, "bytes": len(payload.encode("utf-8"))},
                        line=f"Signed {len(payload)} characters with key {signed['kid'][:8]}")
        return tool_text(signed)

    def call_run_tool(self, grant: GatewayGrant, name: str, arguments: dict,
                      sink: Optional[Sink] = None) -> dict:
        if name == SIGN_TOOL["name"]:
            grant.calls["run"] += 1
            return self._sign(grant, arguments)
        if name not in _RUN_TOOL_NAMES:
            return tool_error("unknown_tool", f"no tool '{name}' on {RUN_SERVER}")
        grant.calls["run"] += 1
        if name in _REPORTS:
            event, key, label = _REPORTS[name]
            said = str(arguments.get(key) or "").strip()
            if not said:
                return tool_error("bad_arguments", f"{name} needs '{key}'")
            data = {key: said[:2000], "role_key": grant.role_key}
            if name == "propose_followup":
                data["body"] = str(arguments.get("body") or "")[:8000]
            self._run_event(grant, event, data, line=f"{label}: {said[:300]}")
            return tool_text({"recorded": True, "event": event})
        if name == "ask_person":
            question = str(arguments.get("question") or "").strip()
            if not question:
                return tool_error("bad_arguments", "ask_person needs 'question'")
            options = [str(o)[:200] for o in (arguments.get("options") or [])
                       if isinstance(o, (str, int, float))][:10]
            decision_id, answer = self._ask(grant, sink, kind=ACTION_ASK_PERSON,
                                            question=question, choices=options)
            if answer["status"] in ("approved", "rejected"):
                return tool_text({"answered": True, "approved": answer["status"] == "approved",
                                  "answer": answer.get("note") or "",
                                  "decision_id": decision_id})
            return _not_released(answer, decision_id, what="your question")
        # request_approval
        action = str(arguments.get("action") or "").strip()
        if not action:
            return tool_error("bad_arguments", "request_approval needs 'action'")
        details = arguments.get("args") if isinstance(arguments.get("args"), dict) else {}
        decision_id, answer = self._ask(grant, sink, kind=ACTION_REQUEST_APPROVAL,
                                        tool=action, arguments=details)
        if answer["status"] == "approved":
            return tool_text({"approved": True, "note": answer.get("note") or "",
                              "decision_id": decision_id})
        return _not_released(answer, decision_id, what=action)

    def _run_event(self, grant: GatewayGrant, event: str, data: dict, *, line: str) -> None:
        from .core import emit_event

        with self._tx() as c:
            emit_event(c, grant.project_id, event, f"agent:{grant.role_key or 'builder'}",
                       "code_task_run", grant.run_key, data)
            try:
                from .agent_turns import capture_enabled, record_turns

                if capture_enabled() and grant.run_id:
                    with c.begin_nested():
                        record_turns(c, [{"direction": "completion", "level": "info",
                                          "content": line}],
                                     project_id=grant.project_id,
                                     code_task_run_id=grant.run_id, role_key=grant.role_key,
                                     engine="mcp_gateway")
            except Exception:  # noqa: BLE001 — the live-feed line never fails the call
                log.debug("mcp gateway: live-feed line not written", exc_info=True)

    # ---- asking and parking ------------------------------------------------------------------
    def _ask(self, grant: GatewayGrant, sink: Optional[Sink], *, kind: str,
             server: Optional[Mapping[str, Any]] = None, tool: str = "",
             arguments: Optional[dict] = None, reason: str = "",
             question: str = "", choices: Optional[list] = None) -> tuple[str, dict]:
        decision_id = self.raise_decision(grant, kind=kind, server=server, tool=tool,
                                          arguments=arguments, reason=reason,
                                          question=question, choices=choices)
        return decision_id, self.park(grant, decision_id, sink)

    def raise_decision(self, grant: GatewayGrant, *, kind: str,
                       server: Optional[Mapping[str, Any]] = None, tool: str = "",
                       arguments: Optional[dict] = None, reason: str = "",
                       question: str = "", choices: Optional[list] = None) -> str:
        """A decisions row a person answers in Approvals, committed before the call parks."""
        from .argument_binding import bind_held_call
        from .core import create_blocking_decision, emit_event

        call_ref = secrets.token_hex(3)
        who = grant.role_key or "The build agent"
        rec: dict[str, Any] = {"action": kind, "run_id": grant.run_key,
                               "code_task_run_id": grant.run_id, "role_key": grant.role_key,
                               "call_ref": call_ref, "via": "mcp_gateway"}
        risk = 2
        if kind == ACTION_TOOL_CALL:
            from .mcp_registry import _declared_tier, tool_tier

            server = dict(server or {})
            risk = tool_tier(server, tool, _declared_tier(server, tool))
            from .tool_policy_engine import always_allow_offer

            rec.update({"server_id": server.get("id"), "server": server.get("name"),
                        "tool": tool, "policy_reason": reason,
                        # "Always allow this" (BOSSAPP-817): offered on a tier 0-1 tool of a
                        # reviewed, unquarantined server; the pin is the tool's schema hash now.
                        "always_allow_offer": always_allow_offer(server, tool, reason=reason),
                        **bind_held_call(tool, arguments or {})})
            ask = (f"{who} wants to call {tool} on {server.get('name')}"
                   f"{' (an unreviewed server)' if reason == 'unreviewed' else ''}. "
                   f"Allow it? [call {call_ref}]")
        elif kind == ACTION_REQUEST_APPROVAL:
            rec.update({"tool": tool, **bind_held_call(tool, arguments or {})})
            ask = f"{who} asks for approval: {tool[:300]} [call {call_ref}]"
        else:
            risk = 1
            rec.update({"question": question[:2000], "choices": list(choices or [])})
            offered = f" Options: {', '.join(choices)}." if choices else ""
            ask = f"{who} asks: {question[:500]}{offered} [call {call_ref}]"
        with self._tx() as c:
            decision_id = create_blocking_decision(
                c, grant.project_id, None, ask, rec, {}, reason=kind,
                risk_tier=max(0, min(int(risk), 3)), default_action="hold")
            emit_event(c, grant.project_id, "code_task.agent_waiting",
                       f"agent:{grant.role_key or 'builder'}", "code_task_run", grant.run_key,
                       {"decision_id": decision_id, "kind": kind, "tool": tool or None,
                        "question": question[:500] or None})
        return decision_id

    def park(self, grant: GatewayGrant, decision_id: str, sink: Optional[Sink] = None,
             *, timeout: Optional[float] = None) -> dict:
        """Hold the call until the decision is answered: {status, note}. status is approved,
        rejected, withdrawn, timed_out or run_ended. The run's stage clock is stopped while it
        waits (stage_clock.parked)."""
        limit = self.ask_timeout if timeout is None else float(timeout)
        started = time.monotonic()
        last_beat = started
        answer: dict = {"status": "timed_out", "note": None}
        with stage_clock.parked(grant.run_key):
            while True:
                status, note = self._decision_status(grant.project_id, decision_id)
                if status == "approved":
                    answer = {"status": "approved", "note": note}
                    break
                if status in ("rejected", "revision_requested"):
                    answer = {"status": "rejected", "note": note}
                    break
                if status in ("withdrawn", "executed", None):
                    answer = {"status": "withdrawn", "note": note}
                    break
                if grant.revoked:
                    answer = {"status": "run_ended", "note": None}
                    break
                now = time.monotonic()
                if now - started >= limit:
                    break
                if sink is not None and now - last_beat >= self.keepalive_every:
                    last_beat = now
                    try:
                        sink.keepalive()
                    except ClientGone:
                        answer = {"status": "run_ended", "note": None}
                        break
                time.sleep(min(self.poll_interval, max(0.01, limit - (now - started))))
        if answer["status"] in ("timed_out", "run_ended"):
            self._withdraw(grant, decision_id, answer["status"])
        with self._tx() as c:
            from .core import emit_event

            emit_event(c, grant.project_id, "code_task.agent_resumed",
                       f"agent:{grant.role_key or 'builder'}", "code_task_run", grant.run_key,
                       {"decision_id": decision_id, "outcome": answer["status"],
                        "waited_seconds": round(time.monotonic() - started, 1)})
        return answer

    def _decision_status(self, project_id: str, decision_id: str
                         ) -> tuple[Optional[str], Optional[str]]:
        with self._tx() as c:
            row = c.execute(text("SELECT status, resolution_note FROM decisions "
                                 "WHERE id = :d AND project_id = :p"),
                            {"d": decision_id, "p": project_id}).mappings().first()
        if row is None:
            return None, None
        return str(row["status"]), row["resolution_note"]

    def _withdraw(self, grant: GatewayGrant, decision_id: str, why: str) -> None:
        """Nobody is waiting on this card any more: take it out of the queue, saying why."""
        note = ("Nobody answered before the build agent's wait ran out, so the call did not run."
                if why == "timed_out" else
                "The build that asked has ended, so there is nothing left to release.")
        with self._tx() as c:
            c.execute(text("UPDATE decisions SET status = 'withdrawn', resolution_note = :n, "
                           "updated_at = now() WHERE id = :d AND status IN "
                           "('pending', 'delegated', 'expired')"),
                      {"d": decision_id, "n": note})


def _not_released(answer: Mapping[str, Any], decision_id: Optional[str], *, what: str) -> dict:
    status = answer.get("status")
    note = answer.get("note")
    if status == "rejected":
        return tool_error("denied_by_person",
                          f"A person declined {what}."
                          + (f" They said: {note}" if note else "")
                          + " Do not retry it as is: change your approach, or call "
                            "report_blocked if you cannot go on without it.",
                          decision_id=decision_id, note=note)
    if status == "timed_out":
        return tool_error("approval_timed_out",
                          f"Nobody answered about {what} in time, so it did not run. Carry on "
                          "without it if you can; otherwise call report_blocked.",
                          decision_id=decision_id)
    if status == "withdrawn":
        return tool_error("denied_by_person", f"The request about {what} was withdrawn, so it "
                          "did not run.", decision_id=decision_id)
    return tool_error("run_ended", f"This run ended while {what} waited, so it did not run.",
                      decision_id=decision_id)


def _tenant_scope(connection: Any, project_id: str) -> tuple[str, str]:
    row = connection.execute(text(
        "SELECT w.org_id, p.workspace_id FROM projects p JOIN workspaces w "
        "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": project_id}).mappings().first()
    if not row:
        raise RuntimeError(f"project {project_id} has no workspace")
    return str(row["org_id"]), str(row["workspace_id"])


class _ProgressSink(Sink):
    """Wraps a sink so keepalives also carry an MCP progress notification when the client asked
    for them (params._meta.progressToken)."""

    def __init__(self, inner: Sink, token: Any) -> None:
        self.inner, self.token, self.n = inner, token, 0

    def keepalive(self) -> None:
        self.n += 1
        self.inner.keepalive()
        self.inner.progress(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": self.token, "progress": self.n,
                       "message": "Waiting for a person's answer in BOSS"}}))

    def progress(self, message: str) -> None:
        self.inner.progress(message)


def _with_progress(sink: Optional[Sink], params: Mapping[str, Any]) -> Optional[Sink]:
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    token = meta.get("progressToken")
    if sink is None or token is None:
        return sink
    return _ProgressSink(sink, token)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _refusal(status: int, reason: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": None, "error": {
        "code": -32001, "message": f"boss mcp gateway: {reason}"}}).encode()


class _SseSink(Sink):
    """A parked call's open stream: comments as keepalives, events for progress."""

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler
        self.started = False

    def open(self) -> None:
        if self.started:
            return
        h = self.handler
        h.send_response(200)
        h.send_header("content-type", "text/event-stream")
        h.send_header("cache-control", "no-cache")
        h.send_header("transfer-encoding", "chunked")
        h.send_header("connection", "close")
        h.end_headers()
        self.started = True

    def write(self, data: bytes) -> None:
        self.open()
        try:
            self.handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientGone(str(exc)) from None

    def keepalive(self) -> None:
        self.write(b": keepalive\n\n")

    def progress(self, message: str) -> None:
        self.write(b"event: message\ndata: " + message.encode() + b"\n\n")

    def finish(self, payload: Any) -> None:
        self.write(b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n")
        try:
            self.handler.wfile.write(b"0\r\n\r\n")
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _handler_for(gateway: McpGateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "boss-mcp-gateway"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 — no access log
            return

        def _send(self, status: int, body: bytes, ctype: str = "application/json",
                  extra: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.close_connection = True

        def _route(self) -> tuple[Optional[GatewayGrant], str]:
            path = self.path.split("?", 1)[0]
            if not path.startswith(PATH_PREFIX):
                self._send(404, _refusal(404, "not_found"))
                return None, ""
            token, _, server_key = path[len(PATH_PREFIX):].partition("/")
            status, reason, grant = gateway.authorise(token)
            if grant is None:
                self._send(status, _refusal(status, reason))
                return None, ""
            return grant, server_key.strip("/") or RUN_SERVER

        def do_POST(self) -> None:  # noqa: N802
            grant, server_key = self._route()
            if grant is None:
                return
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                return self._send(413, _refusal(413, "body_too_large"))
            try:
                message = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._send(400, json.dumps(_rpc_error(None, -32700,
                                                             "parse error")).encode())
            if isinstance(message, list):          # a JSON-RPC batch (protocol 2025-03-26)
                answers = [a for a in (gateway.handle(grant, server_key, m) for m in message
                                       if isinstance(m, dict)) if a is not None]
                if not answers:
                    return self._send(202, b"")
                return self._send(200, json.dumps(answers).encode())
            if not isinstance(message, dict):
                return self._send(400, json.dumps(_rpc_error(None, -32600,
                                                             "invalid request")).encode())
            if "id" not in message:
                return self._send(202, b"")
            streams = "text/event-stream" in (self.headers.get("accept") or "")
            if message.get("method") == "tools/call" and streams:
                # A CALL THAT MAY PARK is answered as a stream: keepalives while a person
                # decides, then the one result event.
                sink = _SseSink(self)
                try:
                    answer = gateway.handle(grant, server_key, message, sink)
                except Exception as exc:  # noqa: BLE001
                    log.warning("mcp gateway call failed: %s", type(exc).__name__,
                                exc_info=True)
                    answer = _rpc_error(message.get("id"), -32603, "internal error")
                try:
                    sink.finish(answer)
                except ClientGone:
                    pass
                self.close_connection = True
                return
            try:
                answer = gateway.handle(grant, server_key, message, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("mcp gateway request failed: %s", type(exc).__name__,
                            exc_info=True)
                answer = _rpc_error(message.get("id"), -32603, "internal error")
            self._send(200, json.dumps(answer).encode())

        def do_GET(self) -> None:  # noqa: N802
            # No server-initiated stream: every answer rides the POST that asked for it.
            grant, _ = self._route()
            if grant is not None:
                self._send(405, b"", extra={"allow": "POST"})

        do_DELETE = do_GET

    return Handler


def _register_redaction(value: str) -> None:
    try:
        from ..identity.redaction import register_secrets

        register_secrets([value])
    except Exception:  # noqa: BLE001 — redaction must never stop a run
        pass


# ── The process's gateway ────────────────────────────────────────────────────────────────────

_GATEWAY: Optional[McpGateway] = None
_GATEWAY_LOCK = threading.Lock()


def get() -> McpGateway:
    global _GATEWAY
    with _GATEWAY_LOCK:
        if _GATEWAY is None:
            _GATEWAY = McpGateway()
        return _GATEWAY


def set_gateway(gateway: Optional[McpGateway]) -> None:
    """Tests: replace the process's gateway (None: a fresh one next time)."""
    global _GATEWAY
    with _GATEWAY_LOCK:
        _GATEWAY = gateway


__all__ = ["ACTION_ASK_PERSON", "ACTION_REQUEST_APPROVAL", "ACTION_TOOL_CALL", "FLAG",
           "GatewayGrant", "McpGateway", "PATH_PREFIX", "RUN_SERVER", "RUN_TOOLS",
           "TOKEN_PREFIX", "Verdict", "enabled", "get", "set_gateway", "tool_error",
           "tool_policy_verdict"]
