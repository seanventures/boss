from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
from typing import Any, Optional

import httpx

from ..config import Settings
from . import outbound_proxy

#: MCP REVISIONS BOSS's client speaks (AP-P5, D10: all three for twelve months).
#:   2026-07-28  MODERN: stateless. No initialize, no session; every request carries its version,
#:               client info and capabilities in `_meta` (and the MCP-Protocol-Version, Mcp-Method,
#:               Mcp-Name headers); long-running calls come back as `io.modelcontextprotocol/tasks`
#:               handles; a server that needs input answers `resultType: "input_required"`.
#:   2025-11-25, 2025-06-18, 2024-11-05  LEGACY: an `initialize` handshake and a session.
#: A server's era is probed ONCE per server and credential with `server/discover` (the
#: spec's dual-era probe) and cached; a legacy server answers the probe with an error, a 4xx
#: or nothing it recognises, and BOSS initializes exactly as it always did.
MODERN_PROTOCOL_VERSION = "2026-07-28"
MODERN_PROTOCOL_VERSIONS = (MODERN_PROTOCOL_VERSION,)
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")
#: What BOSS asks for first in a legacy handshake, and what it falls back to last.
PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]
FALLBACK_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[-1]

#: The extension BOSS declares on every modern request: long-running tool calls may come back
#: as task handles. BOSS declares NO elicitation, sampling or roots capability (Roots and
#: Sampling are deprecated in 2026-07-28; nothing here answers a person's form), so a
#: conformant server never asks it for them.
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
CLIENT_INFO = {"name": "boss", "version": "1.0.0"}
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
#: JSON-RPC code of UnsupportedProtocolVersionError (2026-07-28).
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: How long a server's session is reused before BOSS initializes again.
SESSION_TTL_SECONDS = 600
#: How long a server's probed era is trusted before BOSS probes again.
ERA_TTL_SECONDS = 3600

#: tools/list pages followed before BOSS stops asking (a server that paginates forever).
MAX_TOOL_PAGES = 10
#: A task's suggested polling interval is honoured within these bounds (seconds).
MIN_POLL_SECONDS = 0.05
MAX_POLL_SECONDS = 5.0
TASK_TERMINAL = ("completed", "failed", "cancelled")


class MCPError(RuntimeError):
    """The server answered, and the answer was an error — said in its own words. `code` and
    `data` are the JSON-RPC error's, when it had them."""

    def __init__(self, message: str = "", *, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class MCPToolError(MCPError):
    """tools/call ran and the tool reported failure (`isError`), or its task failed."""


class MCPInputRequired(MCPError):
    """A 2026-07-28 server needs input BOSS's client cannot give by itself: an
    `InputRequiredResult` to a tools/call, or a task waiting in `input_required`.

    `input_requests` is the server's InputRequests map (key -> {method, params}); `request_state`
    the opaque state to echo on a retry of the original call; `task_id` the task waiting (None
    when the call itself asked). The caller surfaces it to a person; it is never answered here."""

    def __init__(self, message: str, *, input_requests: Optional[dict] = None,
                 request_state: Optional[str] = None, task_id: Optional[str] = None):
        super().__init__(message)
        self.input_requests = dict(input_requests or {})
        self.request_state = request_state
        self.task_id = task_id


class MCPTaskPending(MCPError):
    """A task did not reach a terminal status within the client's wait. `task_id` is the durable
    handle: `MCPClient.get_task` / `cancel_task` reach it later."""

    def __init__(self, message: str, *, task_id: str, status: str):
        super().__init__(message)
        self.task_id = task_id
        self.status = status


class _Session:
    __slots__ = ("session_id", "version", "expires")

    def __init__(self, session_id: Optional[str], version: str):
        self.session_id = session_id
        self.version = version
        self.expires = time.monotonic() + SESSION_TTL_SECONDS


class _Era:
    """What a server speaks: modern (stateless, `version`) or legacy (initialize)."""
    __slots__ = ("modern", "version", "expires")

    def __init__(self, modern: bool, version: Optional[str]):
        self.modern = modern
        self.version = version
        self.expires = time.monotonic() + ERA_TTL_SECONDS


_SESSIONS: dict[str, _Session] = {}
_ERAS: dict[str, _Era] = {}
_LOCK = threading.Lock()
_IDS = itertools.count(1)


def _session_key(url: str, headers: Optional[dict]) -> str:
    """One session per server AND credential: two tokens for one URL are two clients."""
    material = json.dumps(sorted((headers or {}).items()))
    return url + "#" + hashlib.sha256(material.encode()).hexdigest()[:16]


def forget_sessions() -> None:
    """Drop every cached session and probed era (tests; a server re-pointed elsewhere)."""
    with _LOCK:
        _SESSIONS.clear()
        _ERAS.clear()


def _next_id() -> int:
    return next(_IDS)


def _sse_messages(body: str) -> list[Any]:
    """The JSON payloads of an SSE stream's `message` events, in order."""
    out: list[Any] = []
    event, data = "message", []
    for raw in body.splitlines() + [""]:
        line = raw.rstrip("\r")
        if not line:
            if data and event == "message":
                try:
                    out.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value or "message"
        elif field == "data":
            data.append(value)
    return out


def _message_for(resp: httpx.Response, rid: Any) -> Any:
    """The JSON-RPC message answering `rid` (plain JSON or an SSE stream), or None."""
    kind = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        messages = _sse_messages(resp.text)
        data = next((m for m in messages if isinstance(m, dict) and m.get("id") == rid), None)
        if data is None:
            data = next((m for m in messages
                         if isinstance(m, dict) and ("result" in m or "error" in m)), None)
        return data
    if not resp.content:
        return {}
    return resp.json()


class MCPClient:
    """A synchronous MCP client over HTTP: Streamable HTTP, and plain JSON-RPC for older servers.

    WHY IT GREW. It POSTed JSON with `Accept: application/json` only, never sent an
    `Mcp-Session-Id`, could not read an SSE answer and took no auth — so it could not talk to a
    single hosted MCP server (GitHub, Linear, Sentry, Context7, Exa: every one speaks Streamable
    HTTP and most want a bearer header). It now:

    * accepts `application/json` and `text/event-stream`, and reads the first `message` event
      that answers its request id;
    * probes each server once (per credential) with `server/discover` to learn its ERA:
      - modern (2026-07-28): stateless — no initialize, no session; every request carries its
        `_meta` (version, client info, capabilities: the tasks extension only) and the
        MCP-Protocol-Version / Mcp-Method / Mcp-Name headers. A tools/call answered with a TASK
        handle is polled (`tasks/get`, at the server's pollIntervalMs) to its end; one answered
        `input_required` raises MCPInputRequired (BOSS never fills a person's form itself), except
        a bare `requestState`, which is echoed once on a retry as the spec allows;
      - legacy (2025-11-25, 2025-06-18, 2024-11-05): initializes once per server and credential,
        keeps the `Mcp-Session-Id` the server returns and sends it (with `MCP-Protocol-Version`)
        on every later call, for ten minutes; a server that forgot the session (404) is
        initialized again once;
    * takes `headers` (the server's auth, resolved by mcp_registry at call time — never stored
      here beyond the session key's hash).

    A server that answers only plain JSON and has no sessions — BOSS's own tools — still works:
    every one of those steps is optional on the server's side.
    """

    def __init__(self, settings: Settings, timeout: float = 20.0,
                 task_timeout: Optional[float] = None):
        self.settings = settings
        self.timeout = timeout
        #: How long call_tool waits for a task to finish before raising MCPTaskPending.
        self.task_timeout = float(task_timeout if task_timeout is not None else timeout)

    # ------------------------------------------------------------------ transport
    def _post(self, url: str, payload: dict, headers: Optional[dict],
              session: Optional[_Session], extra: Optional[dict] = None,
              protocol: Optional[dict] = None) -> httpx.Response:
        # No token passthrough (IDENTITY.md §7.14): an upstream server gets only its OWN stored auth,
        # never a BOSS session, key, run token, DPoP proof or egress credential.
        from boss_engine.identity.tokens import refuse_passthrough

        upstream = refuse_passthrough(headers or {})
        if url.startswith("stdio://"):
            # A STDIO SERVER (BOSSAPP-850): the supervisor runs it in a sandboxed container and
            # bridges it to a loopback streamable-HTTP listener. Its own auth becomes the env
            # var its template names, at spawn; the bridge takes only its per-instance token.
            from .mcp_stdio_supervisor import resolve as resolve_stdio

            url, upstream = resolve_stdio(self.settings, url, upstream)
        if extra:
            # PER-CALL METADATA (BOSSAPP-873, mcp_headers_policy.call_headers): on this request
            # only, never part of the session key; refused like any header if it carries a BOSS
            # credential.
            upstream = {**upstream, **refuse_passthrough(extra)}
        sent = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json", **upstream}
        if session is not None:
            if session.session_id:
                sent["Mcp-Session-Id"] = session.session_id
            sent["MCP-Protocol-Version"] = session.version
        if protocol:
            # The modern request headers (MCP-Protocol-Version, Mcp-Method, Mcp-Name): BOSS's
            # own, set last so no stored or per-call header can impersonate them.
            sent.update(protocol)
        # THE PROXY DECISION (outbound_proxy): NO_PROXY read with its CIDR ranges, which httpx
        # ignores, so an internal server on 10.x is not sent to the company proxy.
        return httpx.post(url, json=payload, headers=sent, timeout=self.timeout,
                          **outbound_proxy.httpx_options(url))

    @staticmethod
    def _answer(resp: httpx.Response, rid: Any) -> Any:
        resp.raise_for_status()
        data = _message_for(resp, rid)
        if data is None:
            raise MCPError("the server's event stream carried no answer to the request")
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                raise MCPError(error.get("message") or "MCP error", code=error.get("code"),
                               data=error.get("data"))
            raise MCPError(str(error) or "MCP error")
        return data.get("result") if isinstance(data, dict) else None

    def _rpc(self, url: str, method: str, params: Optional[dict] = None, rid: Optional[int] = None,
             headers: Optional[dict] = None, session: Optional[_Session] = None,
             extra: Optional[dict] = None) -> Any:
        rid = _next_id() if rid is None else rid
        resp = self._post(url, {"jsonrpc": "2.0", "id": rid, "method": method,
                                "params": params or {}}, headers, session, extra)
        return self._answer(resp, rid)

    # ------------------------------------------------------------------ era (modern / legacy)
    @staticmethod
    def _meta(version: str) -> dict:
        return {META_VERSION: version, META_CLIENT_INFO: dict(CLIENT_INFO),
                META_CLIENT_CAPABILITIES: {"extensions": {TASKS_EXTENSION: {}}}}

    @staticmethod
    def _protocol_headers(version: str, method: str, params: dict) -> dict:
        out = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        name = params.get("name") if method in ("tools/call", "prompts/get") else (
            params.get("uri") if method == "resources/read" else None)
        if isinstance(name, str) and name and "\n" not in name and "\r" not in name:
            out["Mcp-Name"] = name[:200]
        return out

    def _probe(self, url: str, headers: Optional[dict]) -> tuple[_Era, bool]:
        """(era, cacheable). `server/discover` with modern `_meta`: a DiscoverResult naming a
        version BOSS speaks, or an UnsupportedProtocolVersionError listing one, is a modern
        server; anything else a legacy one. 401/403 are raised (the credential is wrong whatever
        the era); a transport failure is a legacy guess that is not cached."""
        rid = _next_id()
        try:
            resp = self._post(url, {"jsonrpc": "2.0", "id": rid, "method": "server/discover",
                                    "params": {"_meta": self._meta(MODERN_PROTOCOL_VERSION)}},
                              headers, None,
                              protocol=self._protocol_headers(MODERN_PROTOCOL_VERSION,
                                                              "server/discover", {}))
        except httpx.TransportError:
            return _Era(False, None), False
        if resp.status_code in (401, 403):
            resp.raise_for_status()
        try:
            data = _message_for(resp, rid)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return _Era(False, None), True
        result, error = data.get("result"), data.get("error")
        offered: list = []
        if isinstance(result, dict) and isinstance(result.get("supportedVersions"), list):
            offered = result["supportedVersions"]
        elif isinstance(error, dict) and error.get("code") == UNSUPPORTED_PROTOCOL_VERSION:
            supported = (error.get("data") or {}).get("supported") \
                if isinstance(error.get("data"), dict) else None
            offered = supported if isinstance(supported, list) else []
        version = next((v for v in MODERN_PROTOCOL_VERSIONS if v in offered), None)
        return _Era(version is not None, version), True

    def _era(self, url: str, headers: Optional[dict]) -> _Era:
        key = _session_key(url, headers)
        with _LOCK:
            cached = _ERAS.get(key)
            if cached is not None and cached.expires > time.monotonic():
                return cached
        era, cacheable = self._probe(url, headers)
        if cacheable:
            with _LOCK:
                _ERAS[key] = era
        return era

    def _forget_era(self, url: str, headers: Optional[dict], era: _Era) -> None:
        with _LOCK:
            _ERAS[_session_key(url, headers)] = era

    def _modern(self, url: str, version: str, method: str, params: dict,
                headers: Optional[dict], extra: Optional[dict] = None) -> Any:
        params = dict(params or {})
        meta = {**(params.get("_meta") or {}), **self._meta(version)}
        trace = {str(k).lower(): v for k, v in (extra or {}).items()}.get("traceparent")
        if trace:
            meta.setdefault("traceparent", trace)   # SEP-414: trace context rides in _meta
        params["_meta"] = meta
        rid = _next_id()
        resp = self._post(url, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                          headers, None, extra,
                          protocol=self._protocol_headers(version, method, params))
        return self._answer(resp, rid)

    # ------------------------------------------------------------------ session (legacy)
    def _initialize(self, url: str, headers: Optional[dict]) -> Optional[_Session]:
        """Open a session, or None when the server will not initialize at all (older plain
        JSON-RPC servers answer tools/list without it)."""
        last: Optional[Exception] = None
        for version in LEGACY_PROTOCOL_VERSIONS:
            try:
                resp = self._post(url, {
                    "jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {},
                               "clientInfo": dict(CLIENT_INFO)},
                }, headers, None)
                result = self._answer(resp, 0) or {}
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (401, 403):
                    raise
                last = error
                continue
            except (MCPError, ValueError) as error:
                last = error
                continue
            agreed = str((result or {}).get("protocolVersion") or version)
            session = _Session(resp.headers.get("mcp-session-id"), agreed)
            try:
                self._post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                           headers, session)
            except httpx.HTTPError:
                pass
            return session
        if isinstance(last, httpx.TransportError):
            raise last
        return None

    def _session(self, url: str, headers: Optional[dict], *, fresh: bool = False
                 ) -> Optional[_Session]:
        key = _session_key(url, headers)
        with _LOCK:
            cached = _SESSIONS.get(key)
            if cached is not None and not fresh and cached.expires > time.monotonic():
                return cached
            _SESSIONS.pop(key, None)
        session = self._initialize(url, headers)
        if session is not None:
            with _LOCK:
                _SESSIONS[key] = session
        return session

    def _legacy(self, url: str, method: str, params: dict, headers: Optional[dict],
                extra: Optional[dict] = None) -> Any:
        session = self._session(url, headers)
        try:
            return self._rpc(url, method, params, headers=headers, session=session, extra=extra)
        except httpx.HTTPStatusError as error:
            # A SESSION THE SERVER NO LONGER KNOWS answers 404 (Streamable HTTP §session
            # management). Initialize again, once.
            if error.response.status_code == 404 and session is not None and session.session_id:
                session = self._session(url, headers, fresh=True)
                return self._rpc(url, method, params, headers=headers, session=session,
                                 extra=extra)
            raise

    def _call(self, url: str, method: str, params: dict, headers: Optional[dict],
              extra: Optional[dict] = None) -> Any:
        era = self._era(url, headers)
        if not era.modern:
            return self._legacy(url, method, params, headers, extra)
        try:
            return self._modern(url, str(era.version), method, params, headers, extra)
        except MCPError as error:
            if error.code != UNSUPPORTED_PROTOCOL_VERSION:
                raise
            # The server dropped the version it advertised: renegotiate from its list, else
            # speak legacy to it from now on.
            supported = (error.data or {}).get("supported") if isinstance(error.data, dict) \
                else None
            version = next((v for v in MODERN_PROTOCOL_VERSIONS
                            if v in (supported or []) and v != era.version), None)
            self._forget_era(url, headers, _Era(version is not None, version))
            if version is not None:
                return self._modern(url, version, method, params, headers, extra)
            return self._legacy(url, method, params, headers, extra)

    def era(self, url: str, headers: Optional[dict] = None) -> dict:
        """What the server speaks, as {"modern": bool, "version": str|None} (probed once)."""
        era = self._era(url, headers)
        return {"modern": era.modern, "version": era.version}

    # ------------------------------------------------------------------ tasks (modern)
    def _modern_only(self, url: str, headers: Optional[dict]) -> str:
        era = self._era(url, headers)
        if not era.modern:
            raise MCPError("tasks are a 2026-07-28 extension; this server speaks an older revision")
        return str(era.version)

    def get_task(self, url: str, task_id: str, headers: Optional[dict] = None) -> dict:
        """tasks/get: the task's current state (DetailedTask)."""
        version = self._modern_only(url, headers)
        return self._modern(url, version, "tasks/get", {"taskId": task_id}, headers) or {}

    def cancel_task(self, url: str, task_id: str, headers: Optional[dict] = None) -> dict:
        """tasks/cancel: cooperative — the task may still end otherwise."""
        version = self._modern_only(url, headers)
        return self._modern(url, version, "tasks/cancel", {"taskId": task_id}, headers) or {}

    def _await_task(self, url: str, task: dict, headers: Optional[dict]) -> dict:
        """Poll a task to a terminal status; its `result` (the tools/call result) when it
        completed. Raises MCPToolError (failed/cancelled), MCPInputRequired, MCPTaskPending."""
        task_id = str(task.get("taskId") or "")
        if not task_id:
            raise MCPError("the server returned a task without a taskId")
        deadline = time.monotonic() + max(0.0, self.task_timeout)
        current = task
        while True:
            status = str(current.get("status") or "working")
            if status == "completed":
                result = current.get("result")
                return result if isinstance(result, dict) else {}
            if status == "failed":
                error = current.get("error") if isinstance(current.get("error"), dict) else {}
                raise MCPToolError(str(error.get("message") or current.get("statusMessage")
                                       or "the task failed")[:2000],
                                   code=error.get("code"), data=error.get("data"))
            if status == "cancelled":
                raise MCPToolError(str(current.get("statusMessage") or "the task was cancelled"))
            if status == "input_required":
                raise MCPInputRequired(
                    str(current.get("statusMessage") or "the task waits for input"),
                    input_requests=current.get("inputRequests")
                    if isinstance(current.get("inputRequests"), dict) else {},
                    task_id=task_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTaskPending(f"task {task_id} is still {status}", task_id=task_id,
                                     status=status)
            interval = current.get("pollIntervalMs")
            wait = (float(interval) / 1000.0) if isinstance(interval, (int, float)) else 1.0
            time.sleep(min(max(wait, MIN_POLL_SECONDS), MAX_POLL_SECONDS, remaining))
            current = self.get_task(url, task_id, headers)

    def _resolved(self, url: str, method: str, params: dict, result: Any,
                  headers: Optional[dict], extra: Optional[dict]) -> Any:
        """A modern result made final: a task awaited; an input_required with only a
        requestState retried once; one that asks for input raised."""
        if not isinstance(result, dict):
            return result
        kind = result.get("resultType")
        if kind == "input_required":
            requests = result.get("inputRequests")
            state = result.get("requestState")
            if not requests and isinstance(state, str) and state:
                retried = self._call(url, method, {**params, "requestState": state}, headers,
                                     extra)
                if isinstance(retried, dict) and retried.get("resultType") == "input_required":
                    raise MCPInputRequired(
                        "the server still needs input",
                        input_requests=retried.get("inputRequests")
                        if isinstance(retried.get("inputRequests"), dict) else {},
                        request_state=retried.get("requestState"))
                return self._resolved(url, method, params, retried, headers, extra) \
                    if isinstance(retried, dict) and retried.get("resultType") == "task" \
                    else retried
            raise MCPInputRequired("the server needs input before it can answer",
                                   input_requests=requests if isinstance(requests, dict) else {},
                                   request_state=state if isinstance(state, str) else None)
        if kind == "task":
            return self._await_task(url, result, headers)
        return result

    # ------------------------------------------------------------------ methods
    def list_tools(self, url: str, headers: Optional[dict] = None) -> list[dict]:
        tools: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(MAX_TOOL_PAGES):
            result = self._call(url, "tools/list", {"cursor": cursor} if cursor else {},
                                headers) or {}
            if not isinstance(result, dict):
                break
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, url: str, name: str, args: dict,
                  headers: Optional[dict] = None, call_headers: Optional[dict] = None) -> Any:
        """``call_headers``: per-call metadata (BOSSAPP-873) sent on this tools/call only."""
        params = {"name": name, "arguments": args or {}}
        result = self._call(url, "tools/call", params, headers, call_headers) or {}
        result = self._resolved(url, "tools/call", params, result, headers, call_headers) or {}
        content = result.get("content") if isinstance(result, dict) else None
        texts = [
            c.get("text", "")
            for c in (content or [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        if isinstance(result, dict) and result.get("isError"):
            raise MCPToolError(("\n".join(texts) or "the tool reported an error")[:2000])
        if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict) \
                and not texts:
            return result["structuredContent"]
        text = "\n".join(texts) if texts else json.dumps(result)
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}
