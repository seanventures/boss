from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

import httpx

from ..config import Settings

#: What BOSS asks for first, and what it falls back to. A server answers `initialize` with the
#: version it will speak; BOSS speaks that one from then on.
PROTOCOL_VERSION = "2025-06-18"
FALLBACK_PROTOCOL_VERSION = "2024-11-05"

#: How long a server's session is reused before BOSS initializes again.
SESSION_TTL_SECONDS = 600

#: tools/list pages followed before BOSS stops asking (a server that paginates forever).
MAX_TOOL_PAGES = 10


class MCPError(RuntimeError):
    """The server answered, and the answer was an error — said in its own words."""


class MCPToolError(MCPError):
    """tools/call ran and the tool reported failure (`isError`)."""


class _Session:
    __slots__ = ("session_id", "version", "expires")

    def __init__(self, session_id: Optional[str], version: str):
        self.session_id = session_id
        self.version = version
        self.expires = time.monotonic() + SESSION_TTL_SECONDS


_SESSIONS: dict[str, _Session] = {}
_LOCK = threading.Lock()


def _session_key(url: str, headers: Optional[dict]) -> str:
    """One session per server AND credential: two tokens for one URL are two clients."""
    material = json.dumps(sorted((headers or {}).items()))
    return url + "#" + hashlib.sha256(material.encode()).hexdigest()[:16]


def forget_sessions() -> None:
    """Drop every cached session (tests; a server re-pointed at another address)."""
    with _LOCK:
        _SESSIONS.clear()


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


class MCPClient:
    """A synchronous MCP client over HTTP: Streamable HTTP, and plain JSON-RPC for older servers.

    WHY IT GREW. It POSTed JSON with `Accept: application/json` only, never sent an
    `Mcp-Session-Id`, could not read an SSE answer and took no auth — so it could not talk to a
    single hosted MCP server (GitHub, Linear, Sentry, Context7, Exa: every one speaks Streamable
    HTTP and most want a bearer header). It now:

    * accepts `application/json` and `text/event-stream`, and reads the first `message` event
      that answers its request id;
    * initializes once per server and credential, keeps the `Mcp-Session-Id` the server returns
      and sends it (with `MCP-Protocol-Version`) on every later call, for ten minutes; a server
      that forgot the session (404) is initialized again once;
    * asks for protocol 2025-06-18 and falls back to 2024-11-05;
    * takes `headers` (the server's auth, resolved by mcp_registry at call time — never stored
      here beyond the session key's hash).

    A server that answers only plain JSON and has no sessions — BOSS's own tools — still works:
    every one of those steps is optional on the server's side.
    """

    def __init__(self, settings: Settings, timeout: float = 20.0):
        self.settings = settings
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _post(self, url: str, payload: dict, headers: Optional[dict],
              session: Optional[_Session]) -> httpx.Response:
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
        sent = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json", **upstream}
        if session is not None:
            if session.session_id:
                sent["Mcp-Session-Id"] = session.session_id
            sent["MCP-Protocol-Version"] = session.version
        return httpx.post(url, json=payload, headers=sent, timeout=self.timeout)

    @staticmethod
    def _answer(resp: httpx.Response, rid: Any) -> Any:
        resp.raise_for_status()
        kind = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            messages = _sse_messages(resp.text)
            data = next((m for m in messages if isinstance(m, dict) and m.get("id") == rid),
                        None)
            if data is None:
                data = next((m for m in messages
                             if isinstance(m, dict) and ("result" in m or "error" in m)), None)
            if data is None:
                raise MCPError("the server's event stream carried no answer to the request")
        else:
            data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            raise MCPError((error.get("message") if isinstance(error, dict) else str(error))
                           or "MCP error")
        return data.get("result") if isinstance(data, dict) else None

    def _rpc(self, url: str, method: str, params: Optional[dict] = None, rid: int = 1,
             headers: Optional[dict] = None, session: Optional[_Session] = None) -> Any:
        resp = self._post(url, {"jsonrpc": "2.0", "id": rid, "method": method,
                                "params": params or {}}, headers, session)
        return self._answer(resp, rid)

    # ------------------------------------------------------------------ session
    def _initialize(self, url: str, headers: Optional[dict]) -> Optional[_Session]:
        """Open a session, or None when the server will not initialize at all (older plain
        JSON-RPC servers answer tools/list without it)."""
        last: Optional[Exception] = None
        for version in (PROTOCOL_VERSION, FALLBACK_PROTOCOL_VERSION):
            try:
                resp = self._post(url, {
                    "jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {},
                               "clientInfo": {"name": "boss", "version": "1.0.0"}},
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

    def _call(self, url: str, method: str, params: dict, headers: Optional[dict]) -> Any:
        session = self._session(url, headers)
        try:
            return self._rpc(url, method, params, rid=1, headers=headers, session=session)
        except httpx.HTTPStatusError as error:
            # A SESSION THE SERVER NO LONGER KNOWS answers 404 (Streamable HTTP §session
            # management). Initialize again, once.
            if error.response.status_code == 404 and session is not None and session.session_id:
                session = self._session(url, headers, fresh=True)
                return self._rpc(url, method, params, rid=1, headers=headers, session=session)
            raise

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
                  headers: Optional[dict] = None) -> Any:
        result = self._call(url, "tools/call", {"name": name, "arguments": args or {}},
                            headers) or {}
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
