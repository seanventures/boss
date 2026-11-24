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
BOSS_MCP_GATEWAY=on|off, ON by default (P8-pre S4, BOSSAPP-1061): every harness's MCP traffic
flows through here and mcp.json carries no upstream credential. Set to a false word (off/0/no/
false), code_task_mcp writes exactly the file it always did (test_mcp_gateway_off_is_byte_identical).

THE HOST-FACING LANE (P8b S12, BOSSAPP-1073; boss-agent-hosts-lld.md §3.10)
---------------------------------------------------------------------------
A microVM guest has no loopback shared with BOSS. Its MCP clients point at `127.0.0.1:7103/mcp/
<server>` with the credential `boss_mcpgw_<run>`; boss-vsockd relays that to boss-hostd, which
identifies the VM by its socket, strips every guest header, MOVES the handle into `X-Boss-Handle`
and forwards to the control plane's `POST /mcp/<server>` with the HOST's identity
(`Authorization: BossHost <proof>`) and its word on the VM, run and epoch (`X-Boss-Vm`,
`X-Boss-Run`, `X-Boss-Fence-Epoch`, `X-Boss-Deployment`) — the model gateway's contract
(HOST_API.md §4.6), applied to MCP (§4.7). Here:

  * the handle is a ROW of `mcp_gateway_handles` (hashed, bound to (run, epoch, vm), ≤ the run's
    life, revoked at run end): a code task's handle is bound to the first VM that redeems it;
    from then on a handle whose row names another VM or run is a REPLAY — revoked, reported as
    `host_events(handle_replay)`, refused `401`; an epoch that is not the handle's, or not the
    run's current one in Postgres, is `409 fenced` within that one request;
  * the row's `servers` is the run's TOOL ALLOWLIST (the agent's grant, agents.held_mcp_servers,
    as code_task_mcp.plan reads it): a server or tool outside it is refused (`403`, typed
    boss.denied_by_policy) before anything is looked up upstream;
  * NO PASSTHROUGH: the guest's handle and headers never reach an upstream. The upstream sign-in
    is read at call time through connection_credentials (per-user / per-agent OAuth with RFC 8707
    `resource`, refreshed when due) and goes on the upstream call only; mcp_client applies
    tokens.refuse_passthrough, the first-party-audience guard, to every upstream header set, so
    a BOSS session, key, run token or egress credential is never sent to a third-party server;
  * STDIO servers that need secrets never run in the guest: the registry's `stdio://` servers
    run under mcp_stdio_supervisor beside this gateway, and the guest sees only the gateway URL;
  * S13: when the host attaches the run's delegated token (`X-Boss-Delegation`, aud `boss:mcp`,
    DPoP-bound to the HOST's key, proof in `DPoP`) it is verified here — a token minted for the
    model gateway, egress or the run plane is refused (`bad_aud`), as is one for another run;
    BOSS_HOST_TOKENS_REQUIRED=1 refuses a host forward that carries none;
  * one `guest_calls` row per call (route `mcp`; batched off the request path), never a body,
    an argument or a handle.

Where an upstream supports token exchange for a tool-scoped token (Entra, Google, AgentCore
3LO), the exchange would happen HERE, never in the guest; none of the built-in upstreams is
exchanged yet — the per-user OAuth token obtained with `resource` = that server is the
tool-scoped token (HOST_API.md §4.7 says which).

`tool_policy_verdict` asks the ordered policy engine (BOSSAPP-815, tool_policy_engine.decide).
"""
from __future__ import annotations

import contextlib
import datetime as _dt
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
#: How long an expired, unrevoked grant is kept so a session that starts late can renew it.
SWEEP_AFTER_EXPIRY_SECONDS = 24 * 3600

#: The decision actions this module raises (decision_actions.RECORDED_ON_APPROVAL: approving
#: records the answer; the parked call reads it — nothing else is started by the card).
ACTION_TOOL_CALL = "gateway_tool_call"
ACTION_ASK_PERSON = "gateway_ask_person"
ACTION_REQUEST_APPROVAL = "gateway_request_approval"

#: The typed errors an agent receives (structuredContent.error.type = "boss.<kind>").
ERROR_KINDS = ("denied_by_policy", "denied_by_person", "approval_timed_out", "run_ended",
               "tool_failed", "unknown_tool", "bad_arguments")


#: The words that turn the gateway OFF. Anything else — unset included — leaves it on.
OFF_WORDS = ("0", "false", "no", "off")


def enabled() -> bool:
    """BOSS_MCP_GATEWAY: ON unless set to a false word (P8-pre S4, BOSSAPP-1061).

    Off by default meant every local run wrote its MCP servers' upstream sign-in into mcp.json,
    where the agent could read it. On, the file holds gateway URLs and a `boss_mcpgw_` run
    token only, and every call leaves a receipt. `off` keeps the old file for an operator who
    needs it."""
    return (os.environ.get(FLAG) or "").strip().lower() not in OFF_WORDS


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
#: BOSSAPP-856: listed only on a build that declared runtime services (boss.verify.yaml runtime:).
RUNTIME_TOOL: dict = {
    "name": "runtime_service",
    "description": ("See and manage the app's runtime services this build runs beside your "
                    "workspace (its database, API, worker): status, start, stop, restart, or "
                    "read a service's recent logs."),
    "inputSchema": {"type": "object", "required": ["action"], "properties": {
        "action": {"type": "string", "enum": ["status", "start", "stop", "restart", "logs"]},
        "service": {"type": "string", "description": "The service's name (all, for status)."},
        "lines": {"type": "integer", "description": "How many log lines (logs only)."}}}}
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
    #: A grant read from `mcp_gateway_handles` (the host-facing lane): `park` re-reads its
    #: revocation from Postgres, since the run may end on another process.
    db_backed: bool = False
    #: The guest's host-side binding, for the audit row (host-facing lane only).
    vm_id: Optional[str] = None
    deployment_id: Optional[str] = None
    org_id: Optional[str] = None
    epoch: Optional[int] = None
    #: The token's lifetime, renewed from each session's start (`session_started`): a build that
    #: waited in the queue before its session began has not spent it (BOSSAPP-1451).
    ttl: float = 0.0

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
        self._host_lane: Optional["HostLane"] = None

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
                             gateway_id=f"mcpgw_{_hash(token)[:16]}", ttl=max(1.0, float(ttl)))
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

    def session_started(self, token: str, run_id: Optional[str]) -> bool:
        """A session that uses this token is starting: bind it to the run (`bind_run`) and give
        the token its full lifetime FROM NOW. The token is issued when the build attempt begins;
        on a small host the attempt can queue for hours before its session starts, and that
        wait used to spend the lifetime (BOSSAPP-1451). Never extends a revoked token and never
        shortens one. True when the token is live for the session."""
        self.bind_run(token, run_id)
        with self._lock:
            grant = self._grants.get(_hash(token))
            if grant is None or grant.revoked:
                return False
            if grant.ttl > 0:
                grant.expires_at = max(grant.expires_at, time.time() + grant.ttl)
            return grant.live()

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
        # An unrevoked token whose build is still queued may lapse before its session starts,
        # which renews it (`session_started`): it is kept for a day past its expiry, so a long
        # queue finds it still there. A revoked one goes once it has expired.
        cutoff = time.time() - SWEEP_AFTER_EXPIRY_SECONDS
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

    # ---- the host-facing lane (P8b S12) ------------------------------------------------------
    @property
    def host_lane(self) -> "HostLane":
        """The host-facing side: a host's forward → a grant; the run's delegated token; audit."""
        lane = self._host_lane
        if lane is None:
            lane = self._host_lane = HostLane(self)
        return lane

    def grant_from_row(self, row: Mapping[str, Any]) -> GatewayGrant:
        """The in-memory grant for a `mcp_gateway_handles` row, cached by hash so call counters
        and a parked call's revocation flag live across requests on this process."""
        with self._lock:
            self._sweep()
            grant = self._grants.get(row["token_hash"])
            expires = (row["expires_at"].timestamp() if row.get("expires_at") is not None
                       else time.time() + GUEST_HANDLE_TTL_SECONDS)
            servers = {str(k): dict(v) for k, v in (row.get("servers") or {}).items()}
            if grant is None:
                grant = GatewayGrant(token_hash=row["token_hash"],
                                     project_id=str(row["project_id"]), expires_at=expires,
                                     servers=servers, role_key=row.get("role_key"),
                                     run_id=str(row["run_id"]), gateway_id=str(row["id"]),
                                     db_backed=True)
                self._grants[grant.token_hash] = grant
            else:
                grant.servers, grant.expires_at = servers, expires
            grant.vm_id, grant.deployment_id = row.get("vm_id"), row.get("deployment_id")
            grant.org_id = str(row["org_id"])
            grant.epoch = int(row.get("fence_epoch") or 0)
            return grant

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
                # Through the run redactor: an upstream's error text can echo the header it
                # refused, and nothing an upstream was sent may reach the guest (S12).
                return tool_error("tool_failed", f"{tool} failed: {_redact(str(exc))[:1500]}",
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
        """Tools only some runs have: `runtime_service` when the build declared runtime services
        (BOSSAPP-856), `sign` when its agent holds a signing key (BOSSAPP-859)."""
        from . import runtime_services

        extra = [RUNTIME_TOOL] if runtime_services.for_run(grant.run_id) is not None else []
        return extra + self._signing_tools(grant)

    def _signing_tools(self, grant: GatewayGrant) -> list[dict]:
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

    def _runtime_service(self, grant: GatewayGrant, arguments: dict) -> dict:
        from . import runtime_services

        sup = runtime_services.for_run(grant.run_id)
        if sup is None:
            return tool_error("no_runtime_services", "this build declares no runtime services")
        try:
            out = sup.act(str(arguments.get("action") or "status"),
                          str(arguments.get("service") or ""),
                          int(arguments.get("lines") or 80))
        except (runtime_services.RuntimeServicesError, ValueError) as error:
            return tool_error("runtime_service_failed", str(error))
        return tool_text(out)

    def call_run_tool(self, grant: GatewayGrant, name: str, arguments: dict,
                      sink: Optional[Sink] = None) -> dict:
        if name == RUNTIME_TOOL["name"]:
            grant.calls["run"] += 1
            return self._runtime_service(grant, arguments)
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
                if grant.revoked or (grant.db_backed and self._db_revoked(grant)):
                    grant.revoked = True
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

    def _db_revoked(self, grant: GatewayGrant) -> bool:
        """A host-facing grant's row, re-read: revoked (run ended on any process) or expired."""
        try:
            with self._tx() as c:
                row = c.execute(text("SELECT revoked_at, expires_at FROM mcp_gateway_handles "
                                     "WHERE token_hash = :h"), {"h": grant.token_hash}
                                ).mappings().first()
        except Exception:  # noqa: BLE001 — unreadable: the in-memory flag decides
            return False
        if row is None or row["revoked_at"] is not None:
            return True
        expires = row["expires_at"]
        return bool(expires is not None and expires.timestamp() <= time.time())

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


# ── The host-facing lane (P8b S12/S13) ───────────────────────────────────────────────────────

HANDLE_HEADER = "x-boss-handle"
VM_HEADER = "x-boss-vm"
RUN_HEADER = "x-boss-run"
EPOCH_HEADER = "x-boss-fence-epoch"
DEPLOYMENT_HEADER = "x-boss-deployment"
TRACE_HEADER = "traceparent"
#: S13: the run's delegated, audience-restricted (boss:mcp), DPoP-bound token the HOST attaches,
#: and the proof its key signed for this very request.
DELEGATION_HEADER = "x-boss-delegation"
DPOP_HEADER = "dpop"
HOST_TOKENS_REQUIRED_ENV = "BOSS_HOST_TOKENS_REQUIRED"
#: A guest handle outlives no run: the longest session BOSS starts, plus grace.
GUEST_HANDLE_TTL_SECONDS = MIN_TTL_SECONDS + TTL_GRACE_SECONDS
#: Where the guest reaches this gateway (contract v1.1 `endpoints.mcp`, boss-vsockd port 7103).
GUEST_MCP_BASE = "http://127.0.0.1:7103/mcp"
#: The control-plane path boss-hostd forwards to (desired-state `endpoints.mcp_gateway`).
HOST_ROUTE = "/mcp"
#: The MCP methods a refused or audited call is named by (guest_calls.path_template).
_AUDIT_ROUTE = "mcp"


def host_tokens_required(connection: Any = None, host_id: Optional[str] = None) -> bool:
    """Per host (P8b S13 decision): required once the host advertised `host_tokens`;
    BOSS_HOST_TOKENS_REQUIRED=1 forces it on everywhere (fleet.host_tokens_required)."""
    from .fleet import host_tokens_required as required

    return required(connection, host_id)


class Refused(Exception):
    """A host forward that goes no further: `status` and the named `reason` the host relays."""

    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.status, self.reason = status, reason


@dataclass
class HostCaller:
    """Who forwarded the request: an enrolled host, with its word on the VM, run and epoch."""

    host_id: str
    org_id: Optional[str]
    vm_id: Optional[str] = None
    run_id: Optional[str] = None
    epoch: Optional[int] = None
    deployment_id: Optional[str] = None
    trace_id: Optional[str] = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str], *, host_id: str, org_id: Optional[str]
                     ) -> "HostCaller":
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        epoch_raw = (h.get(EPOCH_HEADER) or "").strip()
        try:
            epoch = int(epoch_raw) if epoch_raw else None
        except ValueError:
            raise Refused(400, "bad_epoch", "X-Boss-Fence-Epoch is an integer") from None
        vm_id = (h.get(VM_HEADER) or "").strip() or None
        run_id = (h.get(RUN_HEADER) or "").strip() or None
        if not vm_id or not run_id:
            # Without the host's word on the VM and run, the binding and replay checks would be
            # skipped: refused, never resolved on the handle alone.
            raise Refused(400, "binding_headers_required",
                          "a host forward names the VM and the run (X-Boss-Vm, X-Boss-Run)")
        return cls(host_id=host_id, org_id=org_id, vm_id=vm_id, run_id=run_id, epoch=epoch,
                   deployment_id=(h.get(DEPLOYMENT_HEADER) or "").strip() or None,
                   trace_id=(h.get(TRACE_HEADER) or "").strip()[:64] or None)


def presented_handle(headers: Mapping[str, str]) -> str:
    """The guest's handle, as the host moved it: `X-Boss-Handle` only. A handle still in a
    credential header (the strip did not happen) is not read — the host's contract is the move."""
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    return (h.get(HANDLE_HEADER) or "").strip()


# ---- the handle registry (Postgres) ---------------------------------------------------------

def fingerprint(raw: str) -> str:
    """What audit rows carry instead of a handle."""
    return _hash(raw)[:16]


def issue_guest_handle(connection: Any, *, org_id: str, project_id: str, run_id: str, epoch: int,
                       vm_id: Optional[str], servers: Mapping[str, Mapping[str, Any]],
                       role_key: Optional[str] = None, deployment_id: Optional[str] = None,
                       ttl_seconds: int = GUEST_HANDLE_TTL_SECONDS) -> str:
    """Mint a `boss_mcpgw_` handle bound to (run, epoch, vm) with `servers` as its allowlist.
    Returns the raw handle ONCE; only its hash is stored; the value joins the redactor."""
    from ..identity.ids import new_id

    raw = TOKEN_PREFIX + secrets.token_hex(32)
    _register_redaction(raw)
    clean = {str(k): {"server_id": str((v or {}).get("server_id") or ""),
                      "granted_tools": sorted(str(t) for t in ((v or {}).get("granted_tools") or []))}
             for k, v in servers.items()}
    connection.execute(text(
        "INSERT INTO mcp_gateway_handles (id, token_hash, org_id, project_id, run_id, fence_epoch, "
        "vm_id, deployment_id, role_key, servers, expires_at) VALUES (:id, :h, :o, :p, :r, :e, :v, "
        ":d, :k, CAST(:s AS JSONB), now() + make_interval(secs => :ttl))"),
        {"id": new_id("mcph"), "h": _hash(raw), "o": org_id, "p": project_id, "r": run_id,
         "e": int(epoch), "v": vm_id, "d": deployment_id, "k": role_key, "s": json.dumps(clean),
         "ttl": max(60, int(ttl_seconds))})
    return raw


def load_guest_handle(connection: Any, raw: str) -> Optional[dict]:
    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 128:
        return None
    row = connection.execute(text(
        "SELECT id, token_hash, org_id, project_id, run_id, fence_epoch, vm_id, deployment_id, "
        "role_key, servers, expires_at, revoked_at, revoked_reason FROM mcp_gateway_handles "
        "WHERE token_hash = :h"),
        {"h": _hash(raw)}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    if isinstance(out.get("servers"), str):
        out["servers"] = json.loads(out["servers"] or "{}")
    return out


def bind_guest_vm(connection: Any, token_hash: str, vm_id: str) -> str:
    """Bind an unbound handle to the VM that first redeems it; returns the VM it is bound to."""
    connection.execute(text(
        "UPDATE mcp_gateway_handles SET vm_id = :v WHERE token_hash = :h AND vm_id IS NULL"),
        {"h": token_hash, "v": vm_id})
    return str(connection.execute(text(
        "SELECT vm_id FROM mcp_gateway_handles WHERE token_hash = :h"), {"h": token_hash}
    ).scalar() or vm_id)


def revoke_guest_handle(connection: Any, token_hash: str, reason: str) -> None:
    connection.execute(text(
        "UPDATE mcp_gateway_handles SET revoked_at = now(), revoked_reason = :r "
        "WHERE token_hash = :h AND revoked_at IS NULL"), {"h": token_hash, "r": reason[:200]})


def revoke_run_handles(connection: Any, run_id: str, reason: str = "run ended") -> int:
    """Every guest handle of a run (and its children) dies with it; returns how many."""
    try:
        with connection.begin_nested():
            result = connection.execute(text(
                "UPDATE mcp_gateway_handles SET revoked_at = now(), revoked_reason = :re "
                "WHERE (run_id = :r OR run_id LIKE :c) AND revoked_at IS NULL"),
                {"r": run_id, "c": run_id + "/%", "re": reason[:200]})
            return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001 — an install mid-upgrade: the table arrives with the schema
        log.debug("mcp_gateway_handles not available; nothing revoked for %s", run_id)
        return 0


def guest_servers(connection: Any, settings: Any, project_id: str, role_key: Optional[str]
                  ) -> dict[str, dict]:
    """THE RUN'S TOOL ALLOWLIST, from the agent's grant: {cli_name: {server_id, granted_tools}}
    for every server the role holds that the gateway may serve (code_task_mcp.plan, gateway
    lane: Off and ungranted tools are left to the policy seam, which refuses them)."""
    from .code_task_mcp import plan

    role = str(role_key or "").strip().lower()
    if not role:
        return {}
    planned = plan(connection, settings, project_id, role, gateway=True)
    return {str(k): dict(v) for k, v in (planned.get("gateway_servers") or {}).items()
            if k != RUN_SERVER}


def guest_handle(connection: Any, settings: Any, *, org_id: str, project_id: str, run_id: str,
                 epoch: int, vm_id: Optional[str], role_key: Optional[str],
                 deployment_id: Optional[str] = None,
                 base: str = GUEST_MCP_BASE) -> dict[str, Any]:
    """The `mcpgw` handle a guest gets for its run (contract v1.1 `handles.mcpgw`) and the
    `mcp.servers` map it reaches through port 7103 — the granted servers plus the run's own
    `boss_run` tools. Always minted: a run with no granted server still has `boss_run`."""
    servers = guest_servers(connection, settings, project_id, role_key)
    raw = issue_guest_handle(connection, org_id=org_id, project_id=project_id, run_id=run_id,
                             epoch=epoch, vm_id=vm_id, servers=servers, role_key=role_key,
                             deployment_id=deployment_id)
    urls = {name: f"{base}/{name}" for name in servers}
    urls[RUN_SERVER] = f"{base}/{RUN_SERVER}"
    return {"mcpgw": raw, "mcp_servers": urls}


class HostLane:
    """The gateway's host-facing side: resolves a host's forward to a grant, checks the run's
    delegated token, and audits. One per McpGateway (`gateway.host_lane`)."""

    def __init__(self, gateway: "McpGateway") -> None:
        self.gateway = gateway
        self._audit: Any = None
        self._audit_lock = threading.Lock()

    # ---- audit ------------------------------------------------------------------------------
    @property
    def audit(self) -> Any:
        """A guest_calls batch writer (streaming_model_gateway.AuditQueue), started on first use."""
        with self._audit_lock:
            if self._audit is None:
                from .streaming_model_gateway import AuditQueue

                self._audit = AuditQueue(self.gateway.engine())
                self._audit.start()
            return self._audit

    def set_audit(self, queue: Any) -> None:
        with self._audit_lock:
            self._audit = queue

    def record(self, caller: HostCaller, grant: Optional[GatewayGrant], *, server_key: str,
               rpc_method: str, status: int, verdict: str, rejected_by: Optional[str],
               started: float, handle_fp: Optional[str], bytes_in: int = 0,
               bytes_out: int = 0, dest_host: Optional[str] = None) -> None:
        org = (grant.org_id if grant else None) or caller.org_id
        if not org:
            return
        row = {"org_id": org, "host_id": caller.host_id, "call_id": secrets.token_hex(12),
               "route": _AUDIT_ROUTE,
               "at": _dt.datetime.now(_dt.timezone.utc), "vm_id": caller.vm_id or
               (grant.vm_id if grant else None), "run_id": (grant.run_id if grant else None)
               or caller.run_id, "epoch": (grant.epoch if grant else None) or caller.epoch,
               "deployment_id": (grant.deployment_id if grant else None) or caller.deployment_id,
               "dest_host": dest_host, "method": "POST",
               "path_template": f"/mcp/{server_key or '*'}/{rpc_method or '*'}"[:120],
               "status": status, "bytes_in": bytes_in, "bytes_out": bytes_out,
               "duration_ms": int((time.monotonic() - started) * 1000), "handle_fp": handle_fp,
               "verdict": verdict, "rule_id": f"mcp-{server_key}" if server_key else "mcp",
               "rejected_by": f"mcp_gateway:{rejected_by}" if rejected_by else None,
               "trace_id": caller.trace_id}
        try:
            self.audit.put(row)
        except Exception:  # noqa: BLE001 — audit never fails the call
            log.debug("mcp gateway: audit row not queued", exc_info=True)

    # ---- the handle, bound ------------------------------------------------------------------
    def resolve(self, connection: Any, caller: HostCaller, raw: str) -> GatewayGrant:
        """The handle's row, checked against the host's word: wrong org, VM or run → replay
        (revoked, reported, refused); stale epoch (the host's or the run's current one) →
        409 fenced. Returns the in-memory grant the gateway's policy, parking and receipts use."""
        if not raw or not raw.startswith(TOKEN_PREFIX):
            raise Refused(401, "no_handle", "no run handle was presented")
        row = load_guest_handle(connection, raw)
        if row is None:
            raise Refused(401, "unknown_handle")
        if row["revoked_at"] is not None:
            raise Refused(401, "handle_revoked", _lapsed_say("handle_revoked"))
        expires = row["expires_at"]
        if expires is not None and expires.timestamp() <= time.time():
            raise Refused(401, "handle_expired", _lapsed_say("handle_expired"))
        if caller.org_id and row["org_id"] != caller.org_id:
            self._replay(connection, caller, row, "another_org")
        if row["vm_id"] is None and caller.vm_id:
            # A code task's handle is minted before its VM exists: BOUND ON FIRST USE to the VM
            # that redeems it, and enforced from then on (P8b-T8).
            row["vm_id"] = bind_guest_vm(connection, row["token_hash"], caller.vm_id)
        if row["vm_id"] and caller.vm_id and row["vm_id"] != caller.vm_id:
            self._replay(connection, caller, row, "another_vm")
        if caller.run_id and row["run_id"] != caller.run_id:
            self._replay(connection, caller, row, "another_run")
        if caller.epoch is not None and caller.epoch != int(row["fence_epoch"] or 0):
            raise Refused(409, "fenced", f"epoch {caller.epoch} is not the handle's")
        from .streaming_model_gateway import current_epoch

        now_epoch = current_epoch(connection, row["run_id"])
        if now_epoch is not None and now_epoch != int(row["fence_epoch"] or 0):
            raise Refused(409, "fenced", f"the run moved to epoch {now_epoch}")
        return self.gateway.grant_from_row(row)

    def _replay(self, connection: Any, caller: HostCaller, row: Mapping[str, Any], why: str
                ) -> None:
        from . import fleet

        revoke_guest_handle(connection, row["token_hash"], f"handle_replay:{why}")
        with self.gateway._lock:
            cached = self.gateway._grants.get(row["token_hash"])
            if cached is not None:
                cached.revoked = True
        try:
            fleet.host_event(connection, org_id=row["org_id"], host_id=caller.host_id,
                             vm_id=caller.vm_id, kind="handle_replay", actor="mcp_gateway",
                             detail={"why": why, "run_id": row["run_id"], "bound_vm": row["vm_id"],
                                     "handle_fp": row["token_hash"][:16],
                                     "presented_run": caller.run_id})
        except Exception:  # noqa: BLE001 — the refusal stands without the event
            log.warning("mcp handle replay on %s could not be recorded", caller.host_id)
        raise Refused(401, "handle_replay", "that handle belongs to another VM; it is revoked")

    # ---- S13: the run's delegated token ----------------------------------------------------
    def check_delegation(self, connection: Any, headers: Mapping[str, str], *, run_id: str,
                         method: str, url: str, host_id: Optional[str] = None) -> Optional[Any]:
        """`X-Boss-Delegation` + `DPoP`, when the host attached them: a run token minted FOR
        this gateway (aud boss:mcp; one for the model gateway, egress or the run plane is
        `bad_aud`), bound to the host's key (the proof's), naming this run. Returns the claims,
        or None when none was attached and none is required."""
        from ..identity import delegation, host_delegation

        try:
            return host_delegation.check(
                connection, headers, audience=delegation.AUD_MCP, run_id=run_id, method=method,
                url=url, required=host_tokens_required(connection, host_id)
                if not host_delegation.presented(headers) else False)
        except host_delegation.DelegationRefused as exc:
            raise Refused(401, f"run_token_{exc.reason}",
                          "the run's delegated token was refused") from None


def answer_verdict(answer: Any) -> tuple[str, Optional[str]]:
    """What one JSON-RPC answer means for the audit row: ("deny", why) for a JSON-RPC error or a
    tool result the gateway refused (boss.denied_by_policy / denied_by_person / approval_timed_out
    / run_ended / unknown_tool / bad_arguments), ("allow", None) otherwise — a tool that ran and
    failed upstream (boss.tool_failed) was allowed. A batch is a deny when any answer is."""
    if isinstance(answer, list):
        for item in answer:
            verdict, why = answer_verdict(item)
            if verdict == "deny":
                return verdict, why
        return "allow", None
    if not isinstance(answer, dict):
        return "allow", None
    if isinstance(answer.get("error"), dict):
        return "deny", f"rpc_{answer['error'].get('code')}"
    result = answer.get("result")
    if isinstance(result, dict) and result.get("isError"):
        kind = str(((result.get("structuredContent") or {}).get("error") or {}).get("type") or "")
        kind = kind.removeprefix("boss.")
        if kind and kind != "tool_failed":
            return "deny", kind
    return "allow", None


class QueueSink(Sink):
    """A parked call's open stream for an ASGI response: keepalives and progress events are
    queued as SSE bytes; `finish` queues the result and the end marker. The response generator
    drains it; a client that hung up is noticed when the generator is cancelled."""

    def __init__(self) -> None:
        import queue

        self.q: Any = queue.Queue()
        self.gone = threading.Event()

    def _put(self, data: bytes) -> None:
        if self.gone.is_set():
            raise ClientGone("client disconnected")
        self.q.put(data)

    def keepalive(self) -> None:
        self._put(b": keepalive\n\n")

    def progress(self, message: str) -> None:
        self._put(b"event: message\ndata: " + message.encode() + b"\n\n")

    def finish(self, payload: Any) -> None:
        self.q.put(b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n")
        self.q.put(None)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _lapsed_say(reason: str) -> str:
    from .lapsed import say

    return say(reason)


def _refusal(status: int, reason: str) -> bytes:
    from .lapsed import say

    return json.dumps({"jsonrpc": "2.0", "id": None, "error": {
        "code": -32001, "message": f"boss mcp gateway: {say(reason)}"}}).encode()


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


def _redact(value: str) -> str:
    try:
        from ..identity.redaction import redact_text

        return redact_text(value)
    except Exception:  # noqa: BLE001 — the text is better unredacted than the call failed? No:
        return "[redacted: the error text could not be screened]"


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


__all__ = ["ACTION_ASK_PERSON", "ACTION_REQUEST_APPROVAL", "ACTION_TOOL_CALL", "DELEGATION_HEADER",
           "DPOP_HEADER", "FLAG", "GUEST_MCP_BASE", "GatewayGrant", "HANDLE_HEADER", "HOST_ROUTE",
           "HostCaller", "HostLane", "McpGateway", "PATH_PREFIX", "QueueSink", "RUN_SERVER",
           "RUN_TOOLS", "Refused", "TOKEN_PREFIX", "Verdict", "bind_guest_vm", "enabled",
           "fingerprint", "get", "guest_handle", "guest_servers", "host_tokens_required",
           "answer_verdict", "issue_guest_handle", "load_guest_handle", "presented_handle", "revoke_guest_handle",
           "revoke_run_handles", "set_gateway", "tool_error", "tool_policy_verdict"]
