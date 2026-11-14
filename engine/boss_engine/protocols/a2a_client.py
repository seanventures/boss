"""AN A2A v1.0 CLIENT OVER THE JSON-RPC BINDING (AP-P5, BOSSAPP-1056; AGENT_PLATFORM §4.5 "Outbound").

BOSS calls a remote A2A agent with this: SendMessage, SendStreamingMessage (SSE: each `data:` line
is a JSON-RPC response whose `result` is a StreamResponse), GetTask, CancelTask and
CreateTaskPushNotificationConfig, every request carrying `A2A-Version: 1.0` and, when given, the
W3C `traceparent` of the span that made the call (the remote's work joins BOSS's trace).

What comes back is parsed into protocols/a2a_types (Task, Message, events), so a remote agent's
task has the same shape as a BOSS run's. A v0.3 server that does not know a v1.0 method name
(-32601) is asked once more under its v0.3 name (`message/send`, `tasks/get`, ...).

Outbound addresses pass the SSRF guard (services/mcp_oauth.guard_url: https, public addresses,
unless the install allows the host) unless a test transport is injected. The card is fetched and
its signature verified against a JWKS the caller trusts (protocols/agent_card.verify_card).

Not the official A2A SDK (it is not installed in this environment): a minimal, spec-conformant
client of the parts BOSS uses.
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Iterator, Mapping, Optional

import httpx

from . import a2a_types as t
from .agent_card import CardSignatureError, verify_card

_IDS = itertools.count(1)
_V03 = {v: k for k, v in t.LEGACY_METHODS.items()}


class A2AError(RuntimeError):
    """The remote answered with a JSON-RPC error (code, message, data), or not at all."""

    def __init__(self, message: str, *, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code, self.data = code, data


def _guard(url: str, what: str) -> None:
    from ..services.mcp_oauth import UnsafeDestination, guard_url

    try:
        guard_url(url, what=what)
    except UnsafeDestination as exc:
        raise A2AError(str(exc)) from exc


def sse_payloads(lines: Any) -> Iterator[Any]:
    """The JSON payloads of an SSE stream's events (`data:` lines joined per event), in order."""
    data: list = []
    for raw in list(lines) + [""]:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r")
        if not line:
            if data:
                try:
                    yield json.loads("\n".join(data))
                except ValueError:
                    pass
            data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)


def _unwrap(payload: Any) -> Any:
    """A JSON-RPC response's result, or A2AError for its error."""
    if not isinstance(payload, Mapping):
        raise A2AError("the remote agent did not answer in JSON-RPC")
    if payload.get("error"):
        err = payload["error"] if isinstance(payload["error"], Mapping) else {}
        raise A2AError(str(err.get("message") or "A2A error"), code=err.get("code"),
                       data=err.get("data"))
    return payload.get("result") if "result" in payload or "jsonrpc" in payload else payload


class A2AClient:
    """One remote A2A agent, at its JSON-RPC endpoint `base_url` (the card's first JSONRPC
    interface url)."""

    def __init__(self, base_url: str, *, headers: Optional[Mapping[str, str]] = None,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 30.0):
        self.base_url = str(base_url)
        self.headers = dict(headers or {})
        self.transport = transport
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        if self.transport is None:
            _guard(self.base_url, "A2A agent")
        return httpx.Client(transport=self.transport, timeout=self.timeout)

    def _headers(self, traceparent: Optional[str], *, stream: bool = False) -> dict:
        out = {"Content-Type": "application/json", "A2A-Version": t.A2A_VERSION,
               "Accept": "text/event-stream" if stream else "application/json",
               **self.headers}
        if traceparent:
            out["traceparent"] = traceparent
        return out

    def call(self, method: str, params: Mapping[str, Any], *,
             traceparent: Optional[str] = None) -> Any:
        """One JSON-RPC call; the v0.3 name is tried once when the v1.0 name is unknown."""
        for name in (method, _V03.get(method)):
            if not name:
                continue
            body = {"jsonrpc": "2.0", "id": next(_IDS), "method": name, "params": dict(params)}
            try:
                with self._client() as http:
                    resp = http.post(self.base_url, json=body, headers=self._headers(traceparent))
            except httpx.HTTPError as exc:
                raise A2AError(f"the remote agent is unreachable ({type(exc).__name__})") from exc
            try:
                payload = resp.json()
            except ValueError as exc:
                raise A2AError(f"the remote agent answered HTTP {resp.status_code} without "
                               "JSON") from exc
            try:
                return _unwrap(payload)
            except A2AError as exc:
                if exc.code == -32601 and name == method and _V03.get(method):
                    continue
                raise
        raise A2AError(f"the remote agent does not support {method}", code=-32601)

    # ── methods ───────────────────────────────────────────────────────────────────────────

    def send_message(self, request: t.SendMessageRequest, *,
                     traceparent: Optional[str] = None) -> Any:
        """SendMessage: a Task (or, for a direct reply, a Message)."""
        result = self.call("SendMessage", request.to_dict(), traceparent=traceparent)
        return t.parse_stream_response(result or {})

    def stream_message(self, request: t.SendMessageRequest, *,
                       traceparent: Optional[str] = None) -> Iterator[Any]:
        """SendStreamingMessage: Task / Message / status and artifact updates, in order."""
        body = {"jsonrpc": "2.0", "id": next(_IDS), "method": "SendStreamingMessage",
                "params": request.to_dict()}
        try:
            with self._client() as http:
                with http.stream("POST", self.base_url, json=body,
                                 headers=self._headers(traceparent, stream=True)) as resp:
                    kind = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    if kind != "text/event-stream":
                        resp.read()
                        yield t.parse_stream_response(_unwrap(resp.json()) or {})
                        return
                    for payload in sse_payloads(resp.iter_lines()):
                        yield t.parse_stream_response(_unwrap(payload) or {})
        except httpx.HTTPError as exc:
            raise A2AError(f"the remote agent's stream broke ({type(exc).__name__})") from exc

    def get_task(self, task_id: str, *, history_length: Optional[int] = None,
                 traceparent: Optional[str] = None) -> t.Task:
        params: dict = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = int(history_length)
        return t.Task.from_dict(self.call("GetTask", params, traceparent=traceparent) or {})

    def cancel_task(self, task_id: str, *, traceparent: Optional[str] = None) -> t.Task:
        return t.Task.from_dict(self.call("CancelTask", {"id": task_id},
                                          traceparent=traceparent) or {})

    def set_push_config(self, config: t.TaskPushNotificationConfig) -> dict:
        return dict(self.call("CreateTaskPushNotificationConfig",
                              config.to_dict(redact=False)) or {})


def fetch_card(card_url: str, *, transport: Optional[httpx.BaseTransport] = None,
               timeout: float = 15.0) -> dict:
    """GET the agent card (…/.well-known/agent-card.json)."""
    if transport is None:
        _guard(card_url, "agent card")
    try:
        with httpx.Client(transport=transport, timeout=timeout) as http:
            resp = http.get(card_url, headers={"Accept": "application/json",
                                               "A2A-Version": t.A2A_VERSION})
        resp.raise_for_status()
        card = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise A2AError(f"no agent card at {card_url[:200]} ({type(exc).__name__})") from exc
    if not isinstance(card, dict) or not card.get("name"):
        raise A2AError("the agent card is not an AgentCard")
    return card


def jsonrpc_url(card: Mapping[str, Any]) -> Optional[str]:
    """The card's preferred JSON-RPC endpoint (first JSONRPC interface; v0.3 `url` too)."""
    for iface in card.get("supportedInterfaces") or []:
        if isinstance(iface, Mapping) and str(iface.get("protocolBinding") or "").upper() == \
                "JSONRPC" and iface.get("url"):
            return str(iface["url"])
    return str(card["url"]) if card.get("url") else None


def verified_card(card_url: str, jwks: Mapping[str, Any], *,
                  transport: Optional[httpx.BaseTransport] = None) -> dict:
    """The card, only if one of its signatures verifies against `jwks`."""
    card = fetch_card(card_url, transport=transport)
    try:
        verify_card(card, jwks)
    except CardSignatureError as exc:
        raise A2AError(f"the agent card's signature does not verify ({exc.code})") from exc
    return card


__all__ = ["A2AClient", "A2AError", "fetch_card", "jsonrpc_url", "sse_payloads", "verified_card"]
