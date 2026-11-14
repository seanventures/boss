"""A CLIENT FOR AGENT PROTOCOL SERVERS (LangChain's Agent Protocol; LangGraph Agent Server) (AP-P5,
BOSSAPP-1056; AGENT_PLATFORM §4.5 "Outbound": Agent Protocol servers become subagent targets).

Two dialects of one resource model (Threads, Runs), selected by `flavor`:

  agent_protocol  https://github.com/langchain-ai/agent-protocol (openapi.json):
                  POST /threads · POST /runs {thread_id, agent_id, input} · GET /runs/{run_id}
                  · GET /runs/{run_id}/wait · POST /runs/{run_id}/cancel
  langgraph       LangGraph Agent Server: POST /threads · POST /threads/{tid}/runs
                  {assistant_id, input} · GET /threads/{tid}/runs/{rid}
                  · GET /threads/{tid}/runs/{rid}/join · POST /threads/{tid}/runs/{rid}/cancel

Whatever the dialect, a run comes back as an A2A Task (protocols/a2a_types): id = run id,
contextId = thread id, the run status mapped (pending → SUBMITTED, running → WORKING,
success → COMPLETED, error/timeout → FAILED, interrupted → INPUT_REQUIRED), the output as an
artifact. So a LangGraph deployment and an A2A agent look the same to a BOSS sub-agent caller.

Not an official SDK (none is installed here): a minimal client of the endpoints BOSS uses. The
W3C `traceparent` of the calling span is forwarded on every request.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

import httpx

from . import a2a_types as t

FLAVORS = ("agent_protocol", "langgraph")
_STATES = {"pending": t.TaskState.SUBMITTED, "running": t.TaskState.WORKING,
           "success": t.TaskState.COMPLETED, "error": t.TaskState.FAILED,
           "timeout": t.TaskState.FAILED, "interrupted": t.TaskState.INPUT_REQUIRED,
           "cancelled": t.TaskState.CANCELED, "canceled": t.TaskState.CANCELED}


class AgentProtocolError(RuntimeError):
    pass


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, Mapping) and b.get("text"))
    return ""


def output_text(result: Any) -> str:
    """The run's answer: the last assistant/ai message, else the values as JSON."""
    if not isinstance(result, Mapping):
        return "" if result is None else str(result)
    values = result.get("values") if isinstance(result.get("values"), Mapping) else result
    messages = result.get("messages") or (values or {}).get("messages") or []
    for message in reversed(messages if isinstance(messages, list) else []):
        if isinstance(message, Mapping) and str(message.get("role") or message.get("type")
                                                or "") in ("assistant", "ai", "agent"):
            return _text_of(message.get("content"))
    if values and values is not result:
        return json.dumps(values, default=str)[:6000]
    return ""


def task_of(run: Mapping[str, Any], *, output: Optional[str] = None) -> t.Task:
    """A run (either dialect) as an A2A Task."""
    status = str(run.get("status") or "pending")
    state = _STATES.get(status, t.state_for_run_status(status))
    meta = dict(run.get("metadata") or {}) if isinstance(run.get("metadata"), Mapping) else {}
    meta["agent_protocol_status"] = status
    artifacts = [t.Artifact(parts=[t.text_part(output)], name="output")] if output else []
    message = t.agent_message(output or status, context_id=run.get("thread_id"),
                              task_id=run.get("run_id")) if state.interrupted else None
    return t.Task(id=str(run.get("run_id")), context_id=run.get("thread_id"),
                  status=t.TaskStatus(state, message), artifacts=artifacts, metadata=meta)


class AgentProtocolClient:
    def __init__(self, base_url: str, *, agent_id: Optional[str] = None,
                 flavor: str = "agent_protocol", headers: Optional[Mapping[str, str]] = None,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 30.0):
        if flavor not in FLAVORS:
            raise ValueError(f"flavor must be one of {FLAVORS}")
        self.base_url = str(base_url).rstrip("/")
        self.agent_id, self.flavor = agent_id, flavor
        self.headers = dict(headers or {})
        self.transport, self.timeout = transport, timeout

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 traceparent: Optional[str] = None, timeout: Optional[float] = None) -> Any:
        url = self.base_url + path
        if self.transport is None:
            from ..services.mcp_oauth import UnsafeDestination, guard_url

            try:
                guard_url(url, what="Agent Protocol server")
            except UnsafeDestination as exc:
                raise AgentProtocolError(str(exc)) from exc
        headers = {"Accept": "application/json", **self.headers}
        if traceparent:
            headers["traceparent"] = traceparent
        try:
            with httpx.Client(transport=self.transport,
                              timeout=timeout or self.timeout) as http:
                resp = http.request(method, url, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise AgentProtocolError(f"the agent server is unreachable "
                                     f"({type(exc).__name__})") from exc
        if resp.status_code >= 400:
            raise AgentProtocolError(f"the agent server answered HTTP {resp.status_code}")
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            raise AgentProtocolError("the agent server did not answer JSON") from exc

    def create_thread(self, *, metadata: Optional[dict] = None,
                      traceparent: Optional[str] = None) -> str:
        got = self._request("POST", "/threads", json_body={"metadata": metadata or {}},
                            traceparent=traceparent)
        return str(got.get("thread_id"))

    def start_run(self, thread_id: str, input: Any, *, metadata: Optional[dict] = None,
                  traceparent: Optional[str] = None) -> dict:
        if self.flavor == "langgraph":
            body = {"assistant_id": self.agent_id or "agent", "input": input,
                    "metadata": metadata or {}}
            run = self._request("POST", f"/threads/{thread_id}/runs", json_body=body,
                                traceparent=traceparent)
        else:
            body = {"thread_id": thread_id, "input": input, "metadata": metadata or {}}
            if self.agent_id:
                body["agent_id"] = self.agent_id
            run = self._request("POST", "/runs", json_body=body, traceparent=traceparent)
        run = dict(run or {})
        run.setdefault("thread_id", thread_id)
        return run

    def get_run(self, thread_id: str, run_id: str, *,
                traceparent: Optional[str] = None) -> dict:
        path = (f"/threads/{thread_id}/runs/{run_id}" if self.flavor == "langgraph"
                else f"/runs/{run_id}")
        run = dict(self._request("GET", path, traceparent=traceparent) or {})
        run.setdefault("thread_id", thread_id)
        run.setdefault("run_id", run_id)
        return run

    def result(self, thread_id: str, run_id: str, *, traceparent: Optional[str] = None) -> Any:
        """The finished run's values/messages (join / wait)."""
        path = (f"/threads/{thread_id}/runs/{run_id}/join" if self.flavor == "langgraph"
                else f"/runs/{run_id}/wait")
        return self._request("GET", path, traceparent=traceparent)

    def cancel_run(self, thread_id: str, run_id: str, *,
                   traceparent: Optional[str] = None) -> None:
        path = (f"/threads/{thread_id}/runs/{run_id}/cancel" if self.flavor == "langgraph"
                else f"/runs/{run_id}/cancel")
        self._request("POST", path, json_body={}, traceparent=traceparent)

    def task(self, thread_id: str, run_id: str, *, traceparent: Optional[str] = None) -> t.Task:
        """The run as an A2A Task, with its output once it has finished."""
        run = self.get_run(thread_id, run_id, traceparent=traceparent)
        output = None
        if str(run.get("status")) in ("success", "interrupted"):
            output = output_text(self.result(thread_id, run_id, traceparent=traceparent))
        return task_of(run, output=output)


__all__ = ["AgentProtocolClient", "AgentProtocolError", "FLAVORS", "output_text", "task_of"]
