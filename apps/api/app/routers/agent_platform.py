"""Deployments, triggers, bots, health and per-agent endpoints over HTTP (AP-P3, BOSSAPP-1054;
docs/design/AGENT_PLATFORM.md §4.4, §5 Phase 3). ONE router for the phase (design risk: "API
surface sprawl. Keep a single router."). What the wireframes A1-A7 draw:

  A1  GET  /v1/agent-platform/agents                     every agent and bot, live version per
                                                         environment, kind, health, 7-day cost
  A3  GET  /v1/agents/{role_key}/deployments              environments, draft, history
      GET  /v1/agents/{role_key}/promotion-checks         spec, eval set, cost delta, approver,
                                                         runs in flight that stay on their version
      POST /v1/agents/{role_key}/deployments              deploy a version (and its host)
      POST /v1/agents/{role_key}/promote                  dev/staging now; prod via a Decision
      POST /v1/agents/{role_key}/rollback
  A4  GET  /v1/agents/{role_key}/triggers                 + POST to add one
      GET  /v1/agent-triggers/preview                     the next N firings of a schedule
      PATCH/DELETE /v1/agent-triggers/{trigger_id}, POST …/pause, …/resume, …/rotate-secret
      POST /v1/webhooks/inbound/agent/{path_token}        a signed webhook firing (public, HMAC)
  A5  GET  /v1/agent-platform/health                      per deployment, system services, workers
      POST /v1/agents/{role_key}/deployments/{environment}/pause | resume
  A7  GET  /v1/agents/{role_key}/endpoints                REST, MCP tool, A2A (off), callers
      The Agent Protocol: POST /v1/agents/{role_key}/threads, GET /v1/threads/{thread_id}(/history),
      POST /v1/threads/{thread_id}/runs (+ /stream SSE, /wait), GET …/runs/{run_id} (+ /join,
      /stream), POST …/runs/{run_id}/cancel, POST …/runs/{run_id}/fork; stateless
      POST /v1/agents/{role_key}/runs/stream | /runs/wait (a new thread each).
  The worker: POST /v1/bots/tick (the bot clock), GET /v1/workers/scaling.

Permissions: reads `agent:read`; deploy/promote/rollback/triggers `agent:deploy`; pause/resume
`agent:lifecycle`; threads and runs `agent:invoke` (what a service account is given); health
`observability:read`. Interrupts are approved through the Decision API.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit, public
from boss_engine.identity.declarations import PublicReason
from boss_engine.services import agent_triggers as _triggers


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class DeployBody(ProjectBody):
    environment: str
    version: int = Field(ge=1)
    host_id: Optional[str] = None
    host_label: Optional[str] = None


class PromoteBody(ProjectBody):
    from_environment: str = "staging"
    to_environment: str = "prod"
    version: Optional[int] = Field(default=None, ge=1)


class RollbackBody(ProjectBody):
    environment: str
    to_version: Optional[int] = Field(default=None, ge=1)


class TriggerBody(ProjectBody):
    environment: str = "prod"
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    concurrency: str = "skip_if_active"
    name: str = ""
    auto_pause: bool = True


class TriggerPatchBody(ProjectBody):
    config: Optional[dict[str, Any]] = None
    input: Optional[dict[str, Any]] = None
    concurrency: Optional[str] = None
    name: Optional[str] = None
    auto_pause: Optional[bool] = None


class PauseBody(ProjectBody):
    reason: str = ""


class ThreadBody(ProjectBody):
    environment: str = "prod"
    metadata: dict[str, Any] = Field(default_factory=dict)
    double_text: str = "enqueue"


class RunBody(ProjectBody):
    input: Any = None
    double_text: Optional[str] = None
    timeout: float = Field(default=300.0, ge=0, le=3600)


class StatelessRunBody(RunBody):
    environment: str = "prod"


class ForkBody(ProjectBody):
    checkpoint_id: Optional[str] = None
    tool_results: dict[str, Any] = Field(default_factory=dict)


class BotsTickBody(BaseModel):
    project_id: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=500)


def build_agent_platform_router(*, engine, require_write_actor: Callable[..., str],
                                require_reader: Callable[..., None]) -> APIRouter:
    from boss_engine.services import (agent_deployments, agent_health, agent_protocol,
                                      worker_registry)

    router = APIRouter(tags=["agent-platform"])

    def _fail(error: Exception):
        if isinstance(error, (agent_deployments.DeploymentError, _triggers.TriggerError,
                              agent_protocol.ProtocolError)):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        from boss_engine.agents.versions import VersionError

        if isinstance(error, VersionError):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    def _writer(connection, body: Any, permission: str = "workflow_manage") -> str:
        return require_write_actor(connection, body.project_id, body.actor, permission)

    def _trigger_owned(connection, project_id: str, trigger_id: str) -> dict:
        row = _triggers.get(connection, trigger_id, project_id=project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such trigger in this project.")
        return row

    # ── A1 ─────────────────────────────────────────────────────────────────────────────────

    @router.get("/v1/agent-platform/agents")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def agents_overview(project_id: str, environment: Optional[str] = None,
                        kind: Optional[str] = None) -> dict[str, Any]:
        """Every agent and bot: the version live per environment, kind (assistant | bot),
        health, 7-day cost, last run; the four tiles. System services are under health."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_deployments.overview(connection, project_id,
                                                  environment=environment, kind=kind)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agent-platform/mcp-tools")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def mcp_tools(project_id: str) -> dict[str, Any]:
        """The deployed agents an MCP client sees as `agent_<key>` tools."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"tools": agent_protocol.mcp_agents(connection, project_id)}

    # ── A3 ─────────────────────────────────────────────────────────────────────────────────

    @router.get("/v1/agents/{role_key}/deployments")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def agent_deployments_view(role_key: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return agent_deployments.environments_view(connection, project_id, role_key)

    @router.get("/v1/agents/{role_key}/promotion-checks")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def promotion_checks(role_key: str, project_id: str, from_environment: str = "staging",
                         to_environment: str = "prod",
                         version: Optional[int] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_deployments.promotion_checks(
                    connection, project_id, role_key, from_environment=from_environment,
                    to_environment=to_environment, version=version)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/deployments", status_code=201)
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def deploy(role_key: str, body: DeployBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                if body.environment == "prod":
                    raise agent_deployments.DeploymentError(
                        "production takes a version by promotion (POST …/promote), which asks "
                        "for an approval.", status=409)
                return agent_deployments.deploy(connection, body.project_id, role_key,
                                                body.environment, actor=actor,
                                                version=body.version, host_id=body.host_id,
                                                host_label=body.host_label)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/promote")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def promote(role_key: str, body: PromoteBody) -> dict[str, Any]:
        """dev/staging apply now; prod opens a Decision and applies when it is approved. Runs in
        flight stay on the version they started on."""
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return agent_deployments.promote(
                    connection, body.project_id, role_key, actor=actor,
                    from_environment=body.from_environment,
                    to_environment=body.to_environment, version=body.version)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/rollback")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def rollback(role_key: str, body: RollbackBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return agent_deployments.rollback(connection, body.project_id, role_key,
                                                  body.environment, actor=actor,
                                                  to_version=body.to_version)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── A4 ─────────────────────────────────────────────────────────────────────────────────

    @router.get("/v1/agents/{role_key}/triggers")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_triggers(role_key: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            deps = agent_deployments.for_agent(connection, project_id, role_key)
            rows = _triggers.for_deployments(connection, [d["id"] for d in deps])
            return {"agent_key": role_key,
                    "triggers": [_triggers.describe(connection, r) for r in rows],
                    "kinds": list(_triggers.KINDS),
                    "concurrency": list(_triggers.CONCURRENCY)}

    @router.post("/v1/agents/{role_key}/triggers", status_code=201)
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def create_trigger(role_key: str, body: TriggerBody) -> dict[str, Any]:
        """A webhook trigger's signing secret is in this answer once, never again."""
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                dep = agent_deployments.get(connection, body.project_id, role_key,
                                            body.environment)
                if dep is None:
                    raise agent_deployments.DeploymentError(
                        f"{role_key} is not deployed to {body.environment}.", status=404)
                return _triggers.create(connection, project_id=body.project_id,
                                        deployment_id=dep["id"], kind=body.kind, actor=actor,
                                        config=body.config, input=body.input,
                                        concurrency=body.concurrency, name=body.name,
                                        auto_pause=body.auto_pause)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/agent-triggers/preview")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def preview_trigger(project_id: str, kind: str = "cron", cron: Optional[str] = None,
                        every_seconds: Optional[int] = None, timezone: str = "UTC",
                        count: int = 3) -> dict[str, Any]:
        """The next `count` firings of a schedule, as A4 shows them before saving."""
        config: dict[str, Any] = {"timezone": timezone}
        if cron:
            config["cron"] = cron
        if every_seconds:
            config["every_seconds"] = every_seconds
        try:
            return {"kind": kind, "config": config,
                    "next": _triggers.preview(kind, config, count=count)}
        except Exception as error:  # noqa: BLE001
            _fail(error)

    @router.patch("/v1/agent-triggers/{trigger_id}")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def update_trigger(trigger_id: str, body: TriggerPatchBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return _triggers.update(connection, project_id=body.project_id,
                                        trigger_id=trigger_id, actor=actor, config=body.config,
                                        input=body.input, concurrency=body.concurrency,
                                        name=body.name, auto_pause=body.auto_pause)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.delete("/v1/agent-triggers/{trigger_id}")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(query="project_id"))
    def delete_trigger(trigger_id: str, project_id: str,
                       actor: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "workflow_manage")
            if not _triggers.delete(connection, project_id=project_id, trigger_id=trigger_id,
                                    actor=who):
                raise HTTPException(status_code=404, detail="no such trigger in this project.")
            return {"deleted": trigger_id}

    def _pause(trigger_id: str, body: PauseBody, paused: bool) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            _trigger_owned(connection, body.project_id, trigger_id)
            return _triggers.set_paused(connection, project_id=body.project_id,
                                        trigger_id=trigger_id, actor=actor, paused=paused,
                                        reason=body.reason)

    @router.post("/v1/agent-triggers/{trigger_id}/pause")
    @permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
    def pause_trigger(trigger_id: str, body: PauseBody) -> dict[str, Any]:
        """Stops it within one tick: the next tick never selects a paused trigger."""
        return _pause(trigger_id, body, True)

    @router.post("/v1/agent-triggers/{trigger_id}/resume")
    @permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
    def resume_trigger(trigger_id: str, body: PauseBody) -> dict[str, Any]:
        return _pause(trigger_id, body, False)

    @router.post("/v1/agent-triggers/{trigger_id}/rotate-secret")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def rotate_trigger_secret(trigger_id: str, body: ProjectBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return _triggers.rotate_secret(connection, project_id=body.project_id,
                                               trigger_id=trigger_id, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/webhooks/inbound/agent/{path_token}", status_code=202)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=_triggers.receive)
    async def receive_agent_webhook(path_token: str, request: Request):
        """Fire the trigger with this JSON body. Signed like a workflow webhook:
        ``X-BOSS-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`` and
        ``X-BOSS-Timestamp``; ``X-BOSS-Delivery`` is the dedupe key. 202 started, 200
        duplicate or skipped, 404 unknown URL, 401 a bad or missing signature."""
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        with begin(engine) as connection:
            result = _triggers.receive(connection, path_token, raw, headers)
        status = result["status"]
        if status == "not_found":
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        if status == "refused":
            code = 401 if result.get("reason") in ("bad_signature", "missing_signature",
                                                   "stale_timestamp") else 409
            return JSONResponse(status_code=code, content=result)
        return JSONResponse(status_code=202 if status == "started" else 200, content=result)

    # ── A5 ─────────────────────────────────────────────────────────────────────────────────

    @router.get("/v1/agent-platform/health")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def health(project_id: str, hours: int = 24) -> dict[str, Any]:
        """Is every bot alive, fast and within budget: per deployment (state, ok rate, p95,
        cost, last run, last failure in plain words, paused), the system services (D7, not
        agents), the workers and their slots, and the callouts."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return agent_health.view(connection, project_id, hours=max(1, min(hours, 720)))

    def _pause_deployment(role_key: str, environment: str, body: PauseBody,
                          paused: bool) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            dep = agent_deployments.get(connection, body.project_id, role_key, environment)
            if dep is None:
                raise HTTPException(status_code=404, detail="not deployed there.")
            rows = _triggers.for_deployments(connection, [dep["id"]])
            for row in rows:
                if (row["status"] == "paused") != paused:
                    _triggers.set_paused(connection, project_id=body.project_id,
                                         trigger_id=row["id"], actor=actor, paused=paused,
                                         reason=body.reason)
            return {"deployment_id": dep["id"], "paused": paused,
                    "triggers": [r["id"] for r in rows]}

    @router.post("/v1/agents/{role_key}/deployments/{environment}/pause")
    @permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
    def pause_deployment(role_key: str, environment: str, body: PauseBody) -> dict[str, Any]:
        return _pause_deployment(role_key, environment, body, True)

    @router.post("/v1/agents/{role_key}/deployments/{environment}/resume")
    @permit(P.AGENT_LIFECYCLE, scope=Scope.project(body="project_id"))
    def resume_deployment(role_key: str, environment: str, body: PauseBody) -> dict[str, Any]:
        return _pause_deployment(role_key, environment, body, False)

    # ── A7 and the Agent Protocol ──────────────────────────────────────────────────────────

    @router.get("/v1/agents/{role_key}/endpoints")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def endpoints(role_key: str, project_id: str, environment: str = "prod") -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_protocol.endpoints_card(connection, project_id, role_key,
                                                     environment=environment)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    def _invoker(connection, body: Any) -> str:
        return _writer(connection, body, "command_submit")

    @router.post("/v1/agents/{role_key}/threads", status_code=201)
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def create_thread(role_key: str, body: ThreadBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _invoker(connection, body)
            try:
                return agent_protocol.create_thread(
                    connection, project_id=body.project_id, agent_key=role_key, actor=actor,
                    environment=body.environment, metadata=body.metadata,
                    double_text=body.double_text)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/threads/{thread_id}")
    @permit(P.AGENT_INVOKE, scope=Scope.project(query="project_id"))
    def get_thread(thread_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_protocol.get_thread(connection, project_id, thread_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/threads/{thread_id}/history")
    @permit(P.AGENT_INVOKE, scope=Scope.project(query="project_id"))
    def thread_history(thread_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_protocol.thread_history(connection, project_id, thread_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    def _start(thread_id: str, body: RunBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _invoker(connection, body)
            try:
                return agent_protocol.start_run(connection, project_id=body.project_id,
                                                thread_id=thread_id, actor=actor,
                                                input=body.input, double_text=body.double_text)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    def _sse(project_id: str, run_id: str, timeout: float) -> StreamingResponse:
        events = agent_protocol.stream(engine, project_id, run_id, timeout=timeout)
        return StreamingResponse((agent_protocol.sse(e) for e in events),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/v1/threads/{thread_id}/runs", status_code=201)
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def start_run(thread_id: str, body: RunBody) -> dict[str, Any]:
        """Admit a run (it executes on a worker); poll it, join it, or stream it."""
        return _start(thread_id, body)

    @router.post("/v1/threads/{thread_id}/runs/stream")
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def start_run_streaming(thread_id: str, body: RunBody):
        """Admit a run and stream its typed events (SSE: metadata, updates, messages, custom,
        values, end) until it ends or stops for a person."""
        run = _start(thread_id, body)
        return _sse(body.project_id, run["run_id"], body.timeout)

    @router.post("/v1/threads/{thread_id}/runs/wait")
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def start_run_waiting(thread_id: str, body: RunBody) -> dict[str, Any]:
        run = _start(thread_id, body)
        return agent_protocol.join(engine, body.project_id, run["run_id"], timeout=body.timeout)

    def _stateless(role_key: str, body: StatelessRunBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _invoker(connection, body)
            try:
                thread = agent_protocol.create_thread(
                    connection, project_id=body.project_id, agent_key=role_key, actor=actor,
                    environment=body.environment)
                return agent_protocol.start_run(connection, project_id=body.project_id,
                                                thread_id=thread["thread_id"], actor=actor,
                                                input=body.input)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/agents/{role_key}/runs/stream")
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def stateless_stream(role_key: str, body: StatelessRunBody):
        """A run on a new thread, streamed (the A7 "Try it")."""
        run = _stateless(role_key, body)
        return _sse(body.project_id, run["run_id"], body.timeout)

    @router.post("/v1/agents/{role_key}/runs/wait")
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def stateless_wait(role_key: str, body: StatelessRunBody) -> dict[str, Any]:
        """A run on a new thread, answered when it ends or stops for a person (the MCP tool
        `agent_<key>` calls this)."""
        run = _stateless(role_key, body)
        return agent_protocol.join(engine, body.project_id, run["run_id"], timeout=body.timeout)

    @router.get("/v1/threads/{thread_id}/runs/{run_id}")
    @permit(P.AGENT_INVOKE, scope=Scope.project(query="project_id"))
    def get_run(thread_id: str, run_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return agent_protocol.get_run(connection, project_id, run_id, thread_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    def _own_run(project_id: str, thread_id: str, run_id: str) -> None:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                agent_protocol.get_run(connection, project_id, run_id, thread_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/threads/{thread_id}/runs/{run_id}/join")
    @permit(P.AGENT_INVOKE, scope=Scope.project(query="project_id"))
    def join_run(thread_id: str, run_id: str, project_id: str,
                 timeout: float = 60.0) -> dict[str, Any]:
        """Wait for the run to end or stop for a person (at most `timeout` seconds)."""
        _own_run(project_id, thread_id, run_id)
        return agent_protocol.join(engine, project_id, run_id,
                                   timeout=max(0.0, min(timeout, 600.0)))

    @router.get("/v1/threads/{thread_id}/runs/{run_id}/stream")
    @permit(P.AGENT_INVOKE, scope=Scope.project(query="project_id"))
    def join_stream(thread_id: str, run_id: str, project_id: str, timeout: float = 300.0):
        """Stream a run already started (its checkpointed events from the start, then live)."""
        _own_run(project_id, thread_id, run_id)
        return _sse(project_id, run_id, max(0.0, min(timeout, 3600.0)))

    @router.post("/v1/threads/{thread_id}/runs/{run_id}/cancel")
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def cancel_run(thread_id: str, run_id: str, body: ProjectBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _invoker(connection, body)
            try:
                agent_protocol.get_run(connection, body.project_id, run_id, thread_id)
                return agent_protocol.cancel_run(connection, project_id=body.project_id,
                                                 run_id=run_id, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/threads/{thread_id}/runs/{run_id}/fork", status_code=201)
    @permit(P.AGENT_INVOKE, scope=Scope.project(body="project_id"))
    def fork_run(thread_id: str, run_id: str, body: ForkBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _invoker(connection, body)
            try:
                return agent_protocol.fork(connection, project_id=body.project_id,
                                           thread_id=thread_id, run_id=run_id, actor=actor,
                                           checkpoint_id=body.checkpoint_id,
                                           tool_results=body.tool_results)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── The worker ─────────────────────────────────────────────────────────────────────────

    @router.post("/v1/bots/tick")
    @permit(P.SYSTEM_OPERATE, scope=Scope.org())
    def bots_tick(body: BotsTickBody) -> dict[str, Any]:
        """THE BOT CLOCK (the worker's _tick_bots): mirror the migrated mechanisms whose flag is
        on, then fire every due clocked trigger; the runs execute on worker slots."""
        from boss_engine.services import bots

        return bots.tick(engine, project_id=body.project_id, limit=body.limit)

    @router.get("/v1/workers/scaling")
    @permit(P.INFRA_READ, scope=Scope.project(query="project_id"))
    def worker_scaling(project_id: str) -> dict[str, Any]:
        """Slot-based scaling: live workers and slots, agent runs queued, and the signal."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"project_id": project_id,
                    **worker_registry.scaling_signal(connection, project_id),
                    "workers": worker_registry.WorkerRegistryService().list(connection,
                                                                            project_id)}

    return router


__all__ = ["build_agent_platform_router"]
