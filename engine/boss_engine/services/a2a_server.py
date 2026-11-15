"""A DEPLOYED AGENT AS AN A2A v1.0 SERVER (AP-P5, BOSSAPP-1056; AGENT_PLATFORM §4.5 "A2A: serve
real endpoints"; decision D11).

    GET  /a2a/{agent_id}/.well-known/agent-card.json   the agent's card, SIGNED with the agent's
    GET  /.well-known/agent-card.json?agent_id=…        own Ed25519 key (agent_keys), verifiable
                                                        against /v1/agents/{agent_id}/jwks
    POST /a2a/{agent_id}                               JSON-RPC: SendMessage, SendStreamingMessage
                                                        (SSE), GetTask, CancelTask, push configs

ONE RUN, ANOTHER VIEW (principle 6). A Task IS a run of the agent's prod deployment: `contextId`
is its thread, the Task id is the run id, `TASK_STATE_INPUT_REQUIRED` is an open interrupt (a
Decision; services/protocol_answers.py decides who may answer it in-band — a person with step-up;
a key is pointed at the console). Every run is admitted like any other (pinned version, worker,
kernel governance), as the caller: a service account holding `agent:invoke` at the project.

EXPOSURE (D11). Off by default per deployment (`agent_deployments.expose.a2a`). Turning it on is
REFUSED for an agent whose deployed spec holds any write-capable tool until strong isolation
exists (§4.10) — `strong_isolation()` is False until it lands, with no switch to force it — and
for an agent without a signing key (an unsigned card is not served). The same checks run on EVERY
request, so a promotion that adds a write tool closes the endpoint at once.

PUSH NOTIFICATIONS. A client may register a webhook per task (A2A §4.3); when the run stops for a
person or ends, BOSS POSTs the Task as a StreamResponse (`application/a2a+json`, the client's
`Authorization` scheme and token) — once per state change, retried on the next settlement.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Iterator, Mapping, Optional

from sqlalchemy import text

from ..protocols import a2a_types as t

log = logging.getLogger(__name__)

#: How long a blocking SendMessage waits for the run to end or stop for a person.
BLOCKING_WAIT_SECONDS = 60.0
#: How long a SendStreamingMessage stream stays open at most.
STREAM_SECONDS = 600.0
#: The outbound POST of a push notification (tests inject a fake; default httpx, SSRF-guarded).
POST: Optional[Callable[..., Any]] = None


class A2AError(Exception):
    def __init__(self, code: int, message: str, *, data: Optional[dict] = None,
                 http_status: int = 200):
        super().__init__(message)
        self.code, self.message, self.data, self.http_status = code, message, data, http_status

    def error(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


#: BOSS's own JSON-RPC code for "the caller may not answer this interrupt" (outside A2A's range).
ANSWER_REFUSED = -32010


def strong_isolation() -> bool:
    """Whether agents with write tools may be exposed publicly (D11). False until strong isolation
    (§4.10: microVM sandboxes per run) lands — deliberately not configurable.

    The worker's per-run fence (`local_fenced`, skill_executors.NON_STRONG_PROVIDERS) is NOT
    strong isolation: a pool uid behind iptables shares the control plane's kernel. Whatever
    BOSS_GOVERNED_SANDBOX_PROVIDER says, this stays False until a separate kernel per run lands."""
    return False


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── who the agent is, and whether it may be exposed ────────────────────────────────────────────

def resolve(connection: Any, agent_id: str, *, environment: str = "prod") -> Optional[dict]:
    """The agent (by its stable agent id) and its live deployment, or None."""
    from . import agent_deployments

    role = connection.execute(text(
        "SELECT project_id, role_key, display_name, agent_id FROM roles WHERE agent_id = :a "
        "LIMIT 1"), {"a": agent_id}).mappings().first()
    if role is None:
        return None
    dep = agent_deployments.get(connection, role["project_id"], role["role_key"], environment)
    if dep is None or dep.get("status") != "live" or dep.get("runner") != "deployed_agent":
        return None
    return {"agent_id": agent_id, "project_id": role["project_id"],
            "agent_key": role["role_key"], "name": dep.get("display_name") or
            role["display_name"] or role["role_key"], "deployment": dep}


def _compiled(connection: Any, project_id: str, agent_key: str, version: Any) -> Any:
    from ..agents import spec as S
    from ..agents import versions as V

    row = V.get_version(connection, project_id, agent_key, int(version))
    return row, S.compile_spec(S.load_spec(_json(row["spec"], {})))


def write_tools(connection: Any, project_id: str, compiled: Any) -> list:
    """Every tool the compiled spec grants that may change something (D11 counts these):
    a skill whose risk tier is above 0 (or that cannot be found), every MCP grant (an MCP
    server's tools are not known to be read-only), every native loop tool whose catalogue
    permission is not a `:read` (or that is unknown)."""
    from ..config import get_settings

    found: list = []
    try:
        from .entitlements import EntitlementService

        entitlements = EntitlementService(get_settings())
    except Exception:  # noqa: BLE001
        entitlements = None
    for skill in getattr(compiled, "skills", ()) or ():
        tier = None
        if entitlements is not None:
            try:
                tier = int((entitlements.get_skill(skill, project_id, connection=connection)
                            or {}).get("risk_tier") or 0)
            except Exception:  # noqa: BLE001 — unknown is treated as write
                tier = None
        if tier is None or tier > 0:
            found.append(f"skill:{skill}")
    for grant in getattr(compiled, "mcp", ()) or ():
        found.append(f"mcp:{grant.server}")
    native = tuple(getattr(compiled, "native_tools", ()) or ())
    if native:
        from ..agents.versions import native_tool_permissions

        try:
            table = native_tool_permissions()
        except Exception:  # noqa: BLE001 — unknown is treated as write
            table = {}
        for name in native:
            # A native loop tool is a read only when the permission it needs is a read.
            if not str(table.get(name) or "").endswith(":read"):
                found.append(f"native:{name}")
    return found


def exposure_refusal(connection: Any, agent: Mapping[str, Any], *,
                     require_enabled: bool = True) -> Optional[str]:
    """Why this agent may not be served over A2A now, or None."""
    from . import agent_keys

    dep = agent["deployment"]
    if require_enabled and not bool((dep.get("expose") or {}).get("a2a")):
        return "A2A is off for this agent (turn it on per deployment)."
    if not dep.get("version"):
        return "this deployment runs no spec version."
    _, compiled = _compiled(connection, agent["project_id"], agent["agent_key"], dep["version"])
    writes = write_tools(connection, agent["project_id"], compiled)
    if writes and not strong_isolation():
        return ("only read-only agents may be exposed over A2A until strong isolation lands "
                f"(D11); this one holds {', '.join(writes[:6])}.")
    if not agent_keys.has_active_key(connection, agent["project_id"], agent["agent_key"]):
        return "the agent holds no signing key, so its card cannot be signed (create one first)."
    return None


def set_exposure(connection: Any, project_id: str, agent_key: str, environment: str, *,
                 enabled: bool, actor: str) -> dict:
    """Turn A2A on or off for one deployment. On is refused per D11 (see the module docstring)."""
    from . import agent_deployments
    from .agent_deployments import DeploymentError
    from .core import emit_event

    dep = agent_deployments.get(connection, project_id, agent_key, environment)
    if dep is None:
        raise DeploymentError(f"{agent_key} is not deployed to {environment}.", status=404)
    agent_id = connection.execute(text(
        "SELECT agent_id FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": agent_key}).scalar()
    if enabled:
        if environment != "prod":
            raise DeploymentError("A2A serves an agent's prod deployment only.", status=409)
        if not agent_id:
            raise DeploymentError(f"{agent_key} has no agent id yet.", status=409)
        why = exposure_refusal(connection, {"project_id": project_id, "agent_key": agent_key,
                                            "agent_id": agent_id, "deployment": dep},
                               require_enabled=False)
        if why:
            raise DeploymentError(why, status=409)
    connection.execute(text(
        "UPDATE agent_deployments SET expose = expose || jsonb_build_object('a2a', "
        "CAST(:on AS BOOLEAN)), updated_at = now() WHERE id = :d"),
        {"on": bool(enabled), "d": dep["id"]})
    emit_event(connection, project_id, "agent.a2a_exposure_changed", actor, "agent", agent_key,
               {"environment": environment, "enabled": bool(enabled)})
    return {"agent_key": agent_key, "environment": environment, "a2a": bool(enabled),
            "agent_id": agent_id,
            "card": f"/a2a/{agent_id}/.well-known/agent-card.json" if agent_id else None}


# ── the card ───────────────────────────────────────────────────────────────────────────────────

def agent_card(connection: Any, agent: Mapping[str, Any], *, base_url: str) -> dict:
    """The agent's A2A v1.0 AgentCard, signed with its active key (protocols/agent_card)."""
    from ..protocols.agent_card import sign_card
    from . import agent_keys

    base = base_url.rstrip("/")
    dep = agent["deployment"]
    row, compiled = _compiled(connection, agent["project_id"], agent["agent_key"],
                              dep["version"])
    spec = _json(row.get("spec"), {}) or {}
    mission = str(((spec.get("spec") or {}).get("mission")) or agent["name"])
    card = {
        "name": str(agent["name"]),
        "description": mission[:2000],
        "supportedInterfaces": [{"url": f"{base}/a2a/{agent['agent_id']}",
                                 "protocolBinding": "JSONRPC", "protocolVersion": t.A2A_VERSION}],
        "provider": {"organization": "BOSS", "url": base or "/"},
        "version": str(dep.get("version")),
        "capabilities": {"streaming": True, "pushNotifications": True},
        "securitySchemes": {"bossServiceAccount": {"httpAuthSecurityScheme": {
            "scheme": "Bearer",
            "description": "A BOSS service-account key holding agent:invoke in the agent's "
                           "project."}}},
        "securityRequirements": [{"schemes": {"bossServiceAccount": {"list": []}}}],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [{"id": str(agent["agent_key"]), "name": str(agent["name"]),
                    "description": mission[:500],
                    "tags": ["boss", "read-only"],
                    "examples": [str(x) for x in (spec.get("spec") or {}).get("examples") or []][:5]
                    }],
    }
    card["skills"][0] = {k: v for k, v in card["skills"][0].items() if v != []}
    kid, agent_id, signer = agent_keys.jws_signer(connection, agent["project_id"],
                                                  agent["agent_key"])
    return sign_card(card, signer, kid, jku=f"{base}/v1/agents/{agent_id}/jwks")


# ── run ↔ task ─────────────────────────────────────────────────────────────────────────────────

def task_for_run(connection: Any, project_id: str, run_id: str, *,
                 history_length: Optional[int] = None) -> t.Task:
    from . import agent_protocol, protocol_answers

    try:
        run = agent_protocol.get_run(connection, project_id, run_id)
    except agent_protocol.ProtocolError:
        raise A2AError(t.TASK_NOT_FOUND, "task not found") from None
    pending = protocol_answers.pending_for(connection, run_id)
    status = run["status"]
    if status == "interrupted" and not pending:
        state = t.TaskState.WORKING          # answered; the run is resuming
    else:
        state = t.state_for_run_status(status)
    message = None
    artifacts: list = []
    if state is t.TaskState.INPUT_REQUIRED:
        message = t.agent_message(
            "Waiting on a person: " + "; ".join(str(p["question"] or "")[:200]
                                                 for p in pending),
            context_id=run["thread_id"], task_id=run_id,
            data={"decisions": [{"decision_id": p["decision_id"], "question": p["question"],
                                 "kind": p["kind"],
                                 "approve_url": protocol_answers.console_url(p["decision_id"])}
                                for p in pending],
                  "how": "A person approves each decision in BOSS (with a fresh sign-in check); "
                         "then send a message on this task to resume, or wait for the push "
                         "notification."})
    elif state is t.TaskState.COMPLETED:
        output = str(run.get("output") or "")
        artifacts = [t.Artifact(parts=[t.text_part(output)], name="output",
                                artifact_id=f"art_{run_id}")]
        message = t.agent_message(output, context_id=run["thread_id"], task_id=run_id)
    elif state is t.TaskState.FAILED and run.get("error"):
        message = t.agent_message(str(run["error"])[:2000], context_id=run["thread_id"],
                                  task_id=run_id)
    history = []
    row = connection.execute(text("SELECT input FROM agent_runs WHERE id = :r"),
                             {"r": run_id}).scalar()
    for item in _json(row, []) or []:
        if isinstance(item, Mapping) and item.get("role") == "user" and item.get("content"):
            history.append(t.Message(role=t.Role.USER, parts=[t.text_part(str(item["content"]))],
                                     message_id=f"msg_{run_id}_in", context_id=run["thread_id"],
                                     task_id=run_id))
    return t.Task(id=run_id, context_id=run["thread_id"],
                  status=t.TaskStatus(state, message, _iso(run.get("updated_at"))),
                  artifacts=artifacts, history=history[-(history_length or 50):]
                  if history_length != 0 else [],
                  metadata={"agent_key": run.get("agent_key"), "version": run.get("version")})


def _iso(value: Any) -> str:
    text_ = value.isoformat() if hasattr(value, "isoformat") else str(value or "")
    return text_.replace("+00:00", "Z") if text_ else t.TaskStatus(t.TaskState.WORKING).timestamp


def _own_task(connection: Any, agent: Mapping[str, Any], task_id: Any) -> dict:
    row = connection.execute(text(
        "SELECT id, thread_id, deployment_id, agent_key FROM agent_runs WHERE id = :r "
        "AND project_id = :p"), {"r": str(task_id or ""), "p": agent["project_id"]}
    ).mappings().first()
    if row is None or row["agent_key"] != agent["agent_key"] or not row["thread_id"]:
        raise A2AError(t.TASK_NOT_FOUND, "task not found")
    return dict(row)


# ── JSON-RPC ───────────────────────────────────────────────────────────────────────────────────

def check_version(header: Optional[str]) -> None:
    """A2A-Version: 1.0 (or 0.3 / empty, treated as 0.3; both accepted)."""
    value = str(header or "").strip()
    if value and ".".join(value.split(".")[:2]) not in ("1.0", "0.3"):
        raise A2AError(t.VERSION_NOT_SUPPORTED, f"A2A version {value} is not supported",
                       data={"supported": ["1.0", "0.3"]})


def _start(connection: Any, agent: Mapping[str, Any], req: t.SendMessageRequest, *,
           actor: str, auth: Any, traceparent: Optional[str]) -> str:
    from . import agent_protocol

    msg = req.message
    content = msg.text() or (json.dumps(msg.data()[0], default=str) if msg.data() else "")
    if not content.strip():
        raise A2AError(-32602, "the message carries no text or data")
    thread_id = msg.context_id
    try:
        if thread_id:
            owner = connection.execute(text(
                "SELECT agent_key FROM agent_runs WHERE thread_id = :t AND project_id = :p "
                "LIMIT 1"), {"t": thread_id, "p": agent["project_id"]}).scalar()
            thread = agent_protocol.get_thread(connection, agent["project_id"], thread_id)
            if (thread.get("agent_key") or owner) != agent["agent_key"]:
                raise A2AError(-32602, "that contextId belongs to another agent")
        else:
            thread_id = agent_protocol.create_thread(
                connection, project_id=agent["project_id"], agent_key=agent["agent_key"],
                actor=actor, metadata={"surface": "a2a"})["thread_id"]
        run = agent_protocol.start_run(connection, project_id=agent["project_id"],
                                       thread_id=thread_id, actor=actor, input=content,
                                       traceparent=traceparent, trigger="a2a")
    except agent_protocol.ProtocolError as exc:
        if exc.status == 404:
            raise A2AError(-32602, "unknown contextId") from exc
        raise A2AError(-32603 if exc.status >= 500 else -32602, str(exc)) from exc
    push = req.configuration.task_push_notification_config
    if push is not None:
        _save_push(connection, agent, run["run_id"], push, actor=actor)
    return run["run_id"]


def _continue(connection: Any, agent: Mapping[str, Any], run_row: dict,
              req: t.SendMessageRequest, *, auth: Any) -> None:
    """A message on an existing task: an answer to its open interrupt (validated as the caller),
    or a nudge to resume once a person has answered in BOSS."""
    from . import protocol_answers as A

    task = task_for_run(connection, agent["project_id"], run_row["id"])
    if task.state.terminal:
        raise A2AError(t.UNSUPPORTED_OPERATION,
                       "this task has ended; send a new message on its contextId instead")
    for data in req.message.data():
        if not isinstance(data, Mapping) or not data.get("decision_id"):
            continue
        verdict = A.parse_response(data)
        if verdict is None:
            raise A2AError(-32602, "an answer names verdict approve or reject")
        try:
            A.answer(connection, auth, project_id=agent["project_id"],
                     decision_id=str(data["decision_id"]), verdict=verdict["verdict"],
                     note=verdict["note"], answer_value=verdict["answer"], via="a2a")
        except A.AnswerRefused as refused:
            raise A2AError(ANSWER_REFUSED, f"answer refused: {refused.code}",
                           data=refused.body()) from refused
    push = req.configuration.task_push_notification_config
    if push is not None:
        _save_push(connection, agent, run_row["id"], push,
                   actor=str(getattr(getattr(auth, "principal", None), "id", "") or "a2a"))


def handle(engine: Any, agent: Mapping[str, Any], request: Any, *, auth: Any, actor: str,
           traceparent: Optional[str] = None) -> dict:
    """One JSON-RPC request (not the streaming one). Returns the JSON-RPC response."""
    from ..db import begin

    rid = request.get("id") if isinstance(request, Mapping) else None
    try:
        if not isinstance(request, Mapping) or request.get("jsonrpc") != "2.0":
            raise A2AError(-32600, "invalid JSON-RPC request")
        method = t.method_name(str(request.get("method") or ""))
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            raise A2AError(-32602, "params must be an object")
        if method == "SendMessage":
            req = t.SendMessageRequest.from_dict(params)
            with begin(engine) as c:
                if req.message.task_id:
                    row = _own_task(c, agent, req.message.task_id)
                    _continue(c, agent, row, req, auth=auth)
                    run_id = row["id"]
                else:
                    run_id = _start(c, agent, req, actor=actor, auth=auth,
                                    traceparent=traceparent)
            if not req.configuration.return_immediately:
                _wait(engine, agent["project_id"], run_id, BLOCKING_WAIT_SECONDS)
            with begin(engine) as c:
                task = task_for_run(c, agent["project_id"], run_id,
                                    history_length=req.configuration.history_length)
            return _result(rid, {"task": task.to_dict()})
        if method == "GetTask":
            with begin(engine) as c:
                row = _own_task(c, agent, params.get("id"))
                task = task_for_run(c, agent["project_id"], row["id"],
                                    history_length=params.get("historyLength"))
            return _result(rid, task.to_dict())
        if method == "CancelTask":
            from . import agent_protocol

            with begin(engine) as c:
                row = _own_task(c, agent, params.get("id"))
                if task_for_run(c, agent["project_id"], row["id"]).state.terminal:
                    raise A2AError(t.TASK_NOT_CANCELABLE, "the task has already ended")
                agent_protocol.cancel_run(c, project_id=agent["project_id"], run_id=row["id"],
                                          actor=actor)
            notify(engine, row["id"])
            with begin(engine) as c:
                return _result(rid, task_for_run(c, agent["project_id"], row["id"]).to_dict())
        if method == "CreateTaskPushNotificationConfig":
            cfg = t.TaskPushNotificationConfig.from_dict(params)
            with begin(engine) as c:
                row = _own_task(c, agent, cfg.task_id)
                saved = _save_push(c, agent, row["id"], cfg, actor=actor)
            return _result(rid, saved)
        if method in ("GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
                      "DeleteTaskPushNotificationConfig"):
            with begin(engine) as c:
                row = _own_task(c, agent, params.get("taskId"))
                configs = _push_configs(c, row["id"])
                if method == "ListTaskPushNotificationConfigs":
                    return _result(rid, {"configs": configs})
                found = next((x for x in configs if x["id"] == params.get("id")), None)
                if found is None:
                    raise A2AError(t.TASK_NOT_FOUND, "no such push notification config")
                if method == "DeleteTaskPushNotificationConfig":
                    c.execute(text("DELETE FROM a2a_push_configs WHERE id = :i"),
                              {"i": found["id"]})
                    return _result(rid, {})
                return _result(rid, found)
        if method == "GetExtendedAgentCard":
            raise A2AError(t.EXTENDED_AGENT_CARD_NOT_CONFIGURED, "no extended card")
        if method in t.METHODS:
            raise A2AError(t.UNSUPPORTED_OPERATION, f"{method} is not supported here")
        raise A2AError(-32601, f"method not found: {method}")
    except A2AError as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
    except t.A2AShapeError as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}}


def stream(engine: Any, agent: Mapping[str, Any], request: Mapping[str, Any], *, auth: Any,
           actor: str, traceparent: Optional[str] = None) -> Iterator[dict]:
    """SendStreamingMessage: the JSON-RPC responses of the stream, each one StreamResponse —
    the Task first, then a statusUpdate per state change and the output artifact; it closes when
    the task ends or stops for a person (A2A §3.2.3, §11.7)."""
    from ..db import begin

    rid = request.get("id")
    try:
        req = t.SendMessageRequest.from_dict(request.get("params") or {})
        with begin(engine) as c:
            if req.message.task_id:
                row = _own_task(c, agent, req.message.task_id)
                _continue(c, agent, row, req, auth=auth)
                run_id = row["id"]
            else:
                run_id = _start(c, agent, req, actor=actor, auth=auth, traceparent=traceparent)
            task = task_for_run(c, agent["project_id"], run_id)
    except A2AError as exc:
        yield {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
        return
    except t.A2AShapeError as exc:
        yield {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}}
        return
    yield _result(rid, t.stream_response(task))
    last = task.state
    deadline = time.monotonic() + STREAM_SECONDS
    while not (last.terminal or last.interrupted) and time.monotonic() < deadline:
        time.sleep(0.25)
        with begin(engine) as c:
            task = task_for_run(c, agent["project_id"], run_id)
        if task.state is last:
            continue
        last = task.state
        if task.artifacts:
            yield _result(rid, t.stream_response(t.TaskArtifactUpdateEvent(
                task.id, str(task.context_id), task.artifacts[0], last_chunk=True)))
        yield _result(rid, t.stream_response(t.TaskStatusUpdateEvent(
            task.id, str(task.context_id), task.status)))


def _result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _wait(engine: Any, project_id: str, run_id: str, seconds: float) -> None:
    from ..db import begin

    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        with begin(engine) as c:
            state = task_for_run(c, project_id, run_id).state
        if state.terminal or state.interrupted:
            return
        time.sleep(0.25)


# ── push notifications ─────────────────────────────────────────────────────────────────────────

def _org_of(connection: Any, project_id: str) -> str:
    return str(connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar())


def _save_push(connection: Any, agent: Mapping[str, Any], run_id: str,
               cfg: t.TaskPushNotificationConfig, *, actor: str) -> dict:
    from ..identity import vault
    from .core import make_id
    from .mcp_oauth import UnsafeDestination, guard_url

    try:
        guard_url(cfg.url, what="push notification URL")
    except UnsafeDestination as refused:
        raise A2AError(-32602, str(refused)) from refused
    org = _org_of(connection, agent["project_id"])

    def _vault(secret: Optional[str], label: str) -> Optional[str]:
        if not secret:
            return None
        return vault.store(org_id=org, owner_principal_id=None,
                           kind=vault.VaultItemKind.PROVIDER_CREDENTIAL, provider="a2a-push",
                           secret=str(secret), db=connection,
                           connection_name=f"a2a-push:{run_id}:{label}")

    auth = cfg.authentication
    config_id = cfg.id or make_id("a2apush")
    connection.execute(text("""
        INSERT INTO a2a_push_configs (id, project_id, run_id, agent_id, url, token_vault_item_id,
               auth_scheme, credentials_vault_item_id, created_by)
        VALUES (:i, :p, :r, :a, :u, :tok, :scheme, :cred, :by)"""),
        {"i": config_id, "p": agent["project_id"], "r": run_id, "a": agent["agent_id"],
         "u": cfg.url, "tok": _vault(cfg.token, "token"),
         "scheme": auth.scheme if auth else None,
         "cred": _vault(auth.credentials, "credentials") if auth else None, "by": actor})
    return t.TaskPushNotificationConfig(url=cfg.url, id=config_id, task_id=run_id,
                                        authentication=auth).to_dict()


def _push_configs(connection: Any, run_id: str) -> list:
    rows = connection.execute(text(
        "SELECT id, run_id, url, auth_scheme FROM a2a_push_configs WHERE run_id = :r "
        "ORDER BY created_at"), {"r": run_id}).mappings().all()
    return [t.TaskPushNotificationConfig(
        url=r["url"], id=r["id"], task_id=r["run_id"],
        authentication=t.AuthenticationInfo(r["auth_scheme"]) if r["auth_scheme"] else None
    ).to_dict() for r in rows]


def _post(url: str, *, body: bytes, headers: dict) -> int:
    if POST is not None:
        return int(POST(url, content=body, headers=headers))
    import httpx

    from .mcp_oauth import guard_url

    guard_url(url, what="push notification URL")
    return httpx.post(url, content=body, headers=headers, timeout=10.0,
                      follow_redirects=False).status_code


def notify(engine: Any, run_id: str) -> list:
    """Deliver the run's Task to every push config whose last delivered state differs, when the
    run has ended or stopped for a person. Best effort: never raises; returns what it did."""
    from ..db import begin
    from ..identity import vault

    out: list = []
    try:
        with begin(engine) as c:
            if not c.execute(text("SELECT to_regclass('a2a_push_configs')")).scalar():
                return out
            rows = [dict(r) for r in c.execute(text(
                "SELECT * FROM a2a_push_configs WHERE run_id = :r ORDER BY created_at"),
                {"r": run_id}).mappings().all()]
            if not rows:
                return out
            task = task_for_run(c, rows[0]["project_id"], run_id)
            if not (task.state.terminal or task.state.interrupted):
                return out
            secrets: dict = {}
            for row in rows:
                for col in ("token_vault_item_id", "credentials_vault_item_id"):
                    if row.get(col):
                        secrets[row[col]] = vault.read_secret(
                            row[col], db=c, kind=vault.VaultItemKind.PROVIDER_CREDENTIAL)
    except Exception:  # noqa: BLE001 — a notification never fails the run's settlement
        log.warning("a2a push: could not prepare notifications for %s", run_id, exc_info=True)
        return out
    body = json.dumps(t.stream_response(task), default=str).encode("utf-8")
    for row in rows:
        if row.get("last_state") == task.state.value and row.get("last_status") == "delivered":
            continue
        headers = {"Content-Type": t.A2A_MEDIA_TYPE, "A2A-Version": t.A2A_VERSION}
        cred = secrets.get(row.get("credentials_vault_item_id"))
        if row.get("auth_scheme") and cred:
            headers["Authorization"] = f"{row['auth_scheme']} {cred}"
        token = secrets.get(row.get("token_vault_item_id"))
        if token:
            headers["X-A2A-Notification-Token"] = token
        status, error = "failed", None
        try:
            code = _post(row["url"], body=body, headers=headers)
            status = "delivered" if 200 <= code < 300 else "failed"
            error = None if status == "delivered" else f"HTTP {code}"
        except Exception as exc:  # noqa: BLE001
            error = type(exc).__name__
        try:
            with begin(engine) as c:
                c.execute(text("""
                    UPDATE a2a_push_configs SET last_state = :s, last_status = :st,
                           last_error = :e, deliveries = deliveries + 1,
                           last_delivered_at = CASE WHEN :st = 'delivered' THEN now()
                                                    ELSE last_delivered_at END,
                           updated_at = now() WHERE id = :i"""),
                          {"s": task.state.value, "st": status, "e": error, "i": row["id"]})
        except Exception:  # noqa: BLE001
            log.debug("a2a push: bookkeeping failed for %s", row["id"], exc_info=True)
        out.append({"id": row["id"], "state": task.state.value, "status": status})
    return out


__all__ = ["A2AError", "ANSWER_REFUSED", "POST", "agent_card", "exposure_refusal", "handle",
           "notify", "resolve", "set_exposure", "stream", "strong_isolation", "task_for_run",
           "write_tools"]
