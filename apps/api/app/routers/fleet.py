"""Agent hosts, the fleet and the run plane over HTTP (P8a, BOSSAPP-1063;
boss-agent-hosts-lld.md §3.2, §3.13.3). ONE router for the phase.

  HOSTS (boss-hostd → control plane; authenticated as the host, `fleet:host`, system only)
      POST /v1/hosts/enrol                               public: a one-time join token + CSR
      POST /v1/hosts/{agent_host_id}/renew | /heartbeat | /observed | /guest-calls
      GET  /v1/hosts/{agent_host_id}/desired?since=&wait=  level-based watch (long-poll)
  RUN PLANE (boss-hostd on a VM's behalf; `fleet:host`; every run call fenced)
      POST /v1/run-plane/next | /admit
      POST /v1/run-plane/runs/{run_id}/lease                heartbeat | settle | fail
      GET/PUT /v1/run-plane/runs/{run_id}/checkpoints, …/checkpoint-writes
      POST …/interrupts, GET …/interrupts/answers
      POST …/budget/reserve | …/budget/settle | …/ledger | …/ledger/seq | …/spans | …/events
      GET  …/tools, POST …/tools/{name}/invoke              BOSS-state tools (D1a)
  CONSOLE (a person; `fleet:read` / `fleet:manage`, org)
      GET  /v1/fleet/hosts(/{agent_host_id}), POST /v1/fleet/join-tokens (+ DELETE …/{token_id}),
      POST /v1/fleet/hosts/{agent_host_id}/revoke | /cordon | /uncordon, POST /v1/fleet/reconcile
      PUT  /v1/agents/{role_key}/deployments/{environment}/runtime   `agent:deploy`

HOST AUTHENTICATION is decided here in EVERY authz mode (never left to the guard's mode): the
request's verified context must be an enrolled host's (`credential.kind == "host_cert"`,
identity/authn._from_host), and a host acts only for itself and its own replicas.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit, public
from boss_engine.identity.declarations import PublicReason
from boss_engine.services import fleet as _fleet


class EnrolBody(BaseModel):
    join_token: str
    csr_pem: str
    svid_csr_pem: Optional[str] = None
    hostname: str = ""
    backend: str = "firecracker"
    facts: dict[str, Any] = Field(default_factory=dict)
    capacity: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)


class RenewBody(BaseModel):
    csr_pem: str
    svid_csr_pem: Optional[str] = None


class HeartbeatBody(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)
    capacity: Optional[dict[str, Any]] = None
    allocatable: Optional[dict[str, Any]] = None
    pressure: Optional[dict[str, Any]] = None
    replicas: list[dict[str, Any]] = Field(default_factory=list)
    warm_pools: dict[str, Any] = Field(default_factory=dict)
    bgp_state: Optional[str] = None
    observed_generation: int = 0


class ObservedBody(BaseModel):
    replicas: list[dict[str, Any]] = Field(default_factory=list)
    observed_generation: Optional[int] = None


class GuestCallsBody(BaseModel):
    calls: list[dict[str, Any]] = Field(default_factory=list)


class NextBody(BaseModel):
    replica_id: str
    ttl: Optional[int] = None


class AdmitBody(BaseModel):
    replica_id: str
    caller: str = ""
    input: Any = None
    thread_id: Optional[str] = None
    double_text: Optional[str] = None
    traceparent: Optional[str] = None
    ttl: Optional[int] = None


class LeaseBody(BaseModel):
    action: str
    ttl: Optional[int] = None
    outcome: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class RowBody(BaseModel):
    row: dict[str, Any]


class InterruptBody(BaseModel):
    interrupt: dict[str, Any]


class ReserveBody(BaseModel):
    skill_id: str = ""


class SettleBody(BaseModel):
    held: list[str]
    usage: dict[str, Any] = Field(default_factory=dict)
    failed: Optional[dict[str, Any]] = None
    skill_id: str = ""


class LedgerBody(BaseModel):
    event: dict[str, Any]
    state: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class SpanBody(BaseModel):
    span: dict[str, Any]


class EventBody(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class InvokeBody(BaseModel):
    call: dict[str, Any] = Field(default_factory=dict)
    approved: Optional[dict[str, Any]] = None


class JoinTokenBody(BaseModel):
    pool: str = "default"
    labels: dict[str, str] = Field(default_factory=dict)
    hours: int = Field(default=24, ge=1, le=168)
    actor: Optional[str] = None


class RevokeBody(BaseModel):
    reason: str = ""
    actor: Optional[str] = None


class ActorBody(BaseModel):
    actor: Optional[str] = None


class RuntimeBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    runtime: str
    vm_mode: Optional[str] = None
    replicas: Optional[int] = Field(default=None, ge=1, le=64)
    resources: Optional[dict[str, Any]] = None
    image_digest: Optional[str] = None
    placement: Optional[dict[str, Any]] = None


def build_fleet_router(*, engine, require_write_actor: Optional[Callable[..., str]] = None,
                       settings: Optional[Callable[[], Any]] = None) -> APIRouter:
    from boss_engine.services import agent_deployments, fleet_ca, fleet_scheduler, run_plane

    router = APIRouter(tags=["fleet"])

    def _settings() -> Any:
        if settings is not None:
            return settings()
        from boss_engine.config import get_settings

        return get_settings()

    # ── who is calling ────────────────────────────────────────────────────────────────────

    def _auth(request: Request) -> Any:
        from boss_engine.identity import authn

        from ..identity_guard import request_auth

        try:
            return request_auth(request)
        except authn.AuthnError as exc:
            raise HTTPException(status_code=exc.status,
                                detail={"error": exc.code, "message": "host authentication "
                                        "refused"}) from exc

    def _host(request: Request, agent_host_id: Optional[str] = None) -> fleet_ca.HostIdentity:
        ctx = _auth(request)
        cred = getattr(ctx, "credential", None)
        if ctx is None or cred is None or cred.kind != "host_cert":
            raise HTTPException(status_code=401, detail={
                "error": "host_credential_required",
                "message": "this route answers only an enrolled agent host"})
        if agent_host_id is not None and agent_host_id != cred.id:
            raise HTTPException(status_code=403, detail={
                "error": "not_this_host", "message": "a host acts only for itself"})
        return fleet_ca.HostIdentity(cred.id, ctx.principal.org_id, "", "ready")

    def _org(request: Request, connection: Any) -> str:
        ctx = _auth(request)
        if ctx is not None:
            return str(ctx.principal.org_id)
        from sqlalchemy import text

        org = connection.execute(text("SELECT id FROM orgs ORDER BY created_at LIMIT 1")).scalar()
        if not org:
            raise HTTPException(status_code=404, detail="no organisation on this install.")
        return str(org)

    def _who(request: Request, claimed: Optional[str]) -> str:
        ctx = _auth(request)
        return str(ctx.principal.id) if ctx is not None else str(claimed or "operator")

    def _fleet_fail(error: Exception):
        if isinstance(error, _fleet.FleetError):
            raise HTTPException(status_code=error.status,
                                detail={"error": error.code, "message": str(error)}) from error
        if isinstance(error, run_plane.RunPlaneError):
            raise HTTPException(status_code=error.status, detail=error.body()) from error
        if isinstance(error, agent_deployments.DeploymentError):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    def _lease(x_boss_replica: Optional[str], x_boss_job: Optional[str],
               x_boss_run_token: Optional[str],
               x_boss_fence_epoch: Optional[str]) -> run_plane.LeaseRef:
        try:
            return run_plane.LeaseRef(str(x_boss_replica or ""), str(x_boss_job or ""),
                                      str(x_boss_run_token or ""), int(x_boss_fence_epoch or ""))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=run_plane.fenced(
                "a run-plane call carries its lease: X-Boss-Replica, X-Boss-Job, "
                "X-Boss-Run-Token, X-Boss-Fence-Epoch").body()) from exc

    def _run_call(request: Request, run_id: str, lease: run_plane.LeaseRef,
                  fn: Callable[..., dict], **kw: Any) -> dict:
        host = _host(request)
        try:
            with begin(engine) as connection:
                return fn(connection, _settings(), host.host_id, run_id, lease, **kw)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    # ── hosts ─────────────────────────────────────────────────────────────────────────────

    @router.post("/v1/hosts/enrol", status_code=201)
    @public(reason=PublicReason.HOST_JOIN_TOKEN, verifier=_fleet.enrol)
    def enrol(body: EnrolBody) -> dict[str, Any]:
        """A host spends its one-time join token: its 24 h certificate, the CA, its VM block."""
        try:
            with begin(engine) as connection:
                out = _fleet.enrol(connection, join_token=body.join_token, csr_pem=body.csr_pem,
                                   svid_csr_pem=body.svid_csr_pem, hostname=body.hostname,
                                   backend=body.backend, facts=body.facts,
                                   capacity=body.capacity, labels=body.labels)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        fleet_scheduler.reconcile(engine, _settings())
        return out

    @router.post("/v1/hosts/{agent_host_id}/renew")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def renew(agent_host_id: str, body: RenewBody, request: Request) -> dict[str, Any]:
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                return _fleet.renew(connection, host, csr_pem=body.csr_pem,
                                    svid_csr_pem=body.svid_csr_pem)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.post("/v1/hosts/{agent_host_id}/heartbeat")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def heartbeat(agent_host_id: str, body: HeartbeatBody, request: Request) -> dict[str, Any]:
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                out = _fleet.heartbeat(connection, host, body.model_dump())
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        fleet_scheduler.maybe_reconcile(engine, _settings())
        return out

    @router.get("/v1/hosts/{agent_host_id}/desired")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def desired(agent_host_id: str, request: Request, since: int = 0,
                wait: float = 0.0) -> dict[str, Any]:
        """Level-based: the whole desired state once the generation is past `since`."""
        _host(request, agent_host_id)
        try:
            return _fleet.watch(engine, agent_host_id, since=since, wait=wait)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.post("/v1/hosts/{agent_host_id}/observed")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def observed(agent_host_id: str, body: ObservedBody, request: Request) -> dict[str, Any]:
        host = _host(request, agent_host_id)
        with begin(engine) as connection:
            return _fleet.observed(connection, host, body.model_dump())

    @router.post("/v1/hosts/{agent_host_id}/guest-calls")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def guest_calls(agent_host_id: str, body: GuestCallsBody, request: Request) -> dict[str, Any]:
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                return _fleet.ingest_guest_calls(connection, host, body.calls)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    # ── the run plane ─────────────────────────────────────────────────────────────────────

    @router.post("/v1/run-plane/next")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_next(body: NextBody, request: Request) -> dict[str, Any]:
        """The replica's next admitted run, leased to it (or null)."""
        host = _host(request)
        try:
            with begin(engine) as connection:
                return {"run_package": run_plane.next_run(connection, _settings(), host.host_id,
                                                          body.replica_id, ttl=body.ttl)}
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.post("/v1/run-plane/admit")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_admit(body: AdmitBody, request: Request) -> dict[str, Any]:
        """PDP agent:invoke on the forwarded caller + thread_runs.admit + the lease to this
        replica, in one transaction."""
        host = _host(request)
        try:
            with begin(engine) as connection:
                return run_plane.admit(connection, _settings(), host.host_id, body.replica_id,
                                       caller=body.caller, input=body.input,
                                       thread_id=body.thread_id, double_text=body.double_text,
                                       traceparent=body.traceparent, ttl=body.ttl)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.post("/v1/run-plane/runs/{run_id}/lease")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_lease(run_id: str, body: LeaseBody, request: Request,
                 x_boss_replica: Optional[str] = Header(default=None),
                 x_boss_job: Optional[str] = Header(default=None),
                 x_boss_run_token: Optional[str] = Header(default=None),
                 x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        if body.action == "heartbeat":
            return _run_call(request, run_id, lease, run_plane.heartbeat, ttl=body.ttl)
        if body.action in ("settle", "hand_back"):
            outcome = body.outcome if body.action == "settle" else {"status": "drained"}
            out = _run_call(request, run_id, lease, run_plane.settle, outcome=outcome)
            run_plane.after_commit(engine, run_id)
            return out
        if body.action == "fail":
            out = _run_call(request, run_id, lease, run_plane.fail, error=body.error)
            run_plane.after_commit(engine, run_id)
            return out
        raise HTTPException(status_code=400, detail={
            "error": "bad_action", "message": "action is heartbeat, settle, hand_back or fail"})


    @router.get("/v1/run-plane/runs/{run_id}/checkpoints")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_get_checkpoints(run_id: str, request: Request, checkpoint_id: Optional[str] = None,
                           ns: str = "", history: bool = False,
                           x_boss_replica: Optional[str] = Header(default=None),
                           x_boss_job: Optional[str] = Header(default=None),
                           x_boss_run_token: Optional[str] = Header(default=None),
                           x_boss_fence_epoch: Optional[str] = Header(default=None)
                           ) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.get_checkpoints,
                         checkpoint_id=checkpoint_id, ns=ns, history=history)

    @router.put("/v1/run-plane/runs/{run_id}/checkpoints")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_put_checkpoint(run_id: str, body: RowBody, request: Request,
                          x_boss_replica: Optional[str] = Header(default=None),
                          x_boss_job: Optional[str] = Header(default=None),
                          x_boss_run_token: Optional[str] = Header(default=None),
                          x_boss_fence_epoch: Optional[str] = Header(default=None)
                          ) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.put_checkpoint, row=body.row)

    @router.get("/v1/run-plane/runs/{run_id}/checkpoint-writes")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_get_writes(run_id: str, checkpoint_id: str, request: Request,
                      x_boss_replica: Optional[str] = Header(default=None),
                      x_boss_job: Optional[str] = Header(default=None),
                      x_boss_run_token: Optional[str] = Header(default=None),
                      x_boss_fence_epoch: Optional[str] = Header(default=None)
                      ) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.get_writes,
                         checkpoint_id=checkpoint_id)

    @router.put("/v1/run-plane/runs/{run_id}/checkpoint-writes")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_put_write(run_id: str, body: RowBody, request: Request,
                     x_boss_replica: Optional[str] = Header(default=None),
                     x_boss_job: Optional[str] = Header(default=None),
                     x_boss_run_token: Optional[str] = Header(default=None),
                     x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.put_write, row=body.row)

    @router.post("/v1/run-plane/runs/{run_id}/interrupts")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_interrupt(run_id: str, body: InterruptBody, request: Request,
                     x_boss_replica: Optional[str] = Header(default=None),
                     x_boss_job: Optional[str] = Header(default=None),
                     x_boss_run_token: Optional[str] = Header(default=None),
                     x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.open_interrupt, item=body.interrupt)

    @router.get("/v1/run-plane/runs/{run_id}/interrupts/answers")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_answers(run_id: str, request: Request,
                   x_boss_replica: Optional[str] = Header(default=None),
                   x_boss_job: Optional[str] = Header(default=None),
                   x_boss_run_token: Optional[str] = Header(default=None),
                   x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.interrupt_answers)

    @router.post("/v1/run-plane/runs/{run_id}/budget/reserve")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_reserve(run_id: str, body: ReserveBody, request: Request,
                   x_boss_replica: Optional[str] = Header(default=None),
                   x_boss_job: Optional[str] = Header(default=None),
                   x_boss_run_token: Optional[str] = Header(default=None),
                   x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.budget_reserve,
                         skill_id=body.skill_id)

    @router.post("/v1/run-plane/runs/{run_id}/budget/settle")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_settle_budget(run_id: str, body: SettleBody, request: Request,
                         x_boss_replica: Optional[str] = Header(default=None),
                         x_boss_job: Optional[str] = Header(default=None),
                         x_boss_run_token: Optional[str] = Header(default=None),
                         x_boss_fence_epoch: Optional[str] = Header(default=None)
                         ) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.budget_settle, held=body.held,
                         usage=body.usage, failed=body.failed, skill_id=body.skill_id)

    @router.post("/v1/run-plane/runs/{run_id}/ledger")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_ledger(run_id: str, body: LedgerBody, request: Request,
                  x_boss_replica: Optional[str] = Header(default=None),
                  x_boss_job: Optional[str] = Header(default=None),
                  x_boss_run_token: Optional[str] = Header(default=None),
                  x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.ledger_write, event=body.event,
                         state=body.state, meta=body.meta)

    @router.post("/v1/run-plane/runs/{run_id}/ledger/seq")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_ledger_seq(run_id: str, request: Request,
                      x_boss_replica: Optional[str] = Header(default=None),
                      x_boss_job: Optional[str] = Header(default=None),
                      x_boss_run_token: Optional[str] = Header(default=None),
                      x_boss_fence_epoch: Optional[str] = Header(default=None)
                      ) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.ledger_seq)

    @router.post("/v1/run-plane/runs/{run_id}/spans")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_spans(run_id: str, body: SpanBody, request: Request,
                 x_boss_replica: Optional[str] = Header(default=None),
                 x_boss_job: Optional[str] = Header(default=None),
                 x_boss_run_token: Optional[str] = Header(default=None),
                 x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.span_write, span=body.span)

    @router.post("/v1/run-plane/runs/{run_id}/events")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_events(run_id: str, body: EventBody, request: Request,
                  x_boss_replica: Optional[str] = Header(default=None),
                  x_boss_job: Optional[str] = Header(default=None),
                  x_boss_run_token: Optional[str] = Header(default=None),
                  x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.event_write, type=body.type,
                         data=body.data)

    @router.get("/v1/run-plane/runs/{run_id}/tools")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_tools(run_id: str, request: Request,
                 x_boss_replica: Optional[str] = Header(default=None),
                 x_boss_job: Optional[str] = Header(default=None),
                 x_boss_run_token: Optional[str] = Header(default=None),
                 x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.tool_schemas)

    @router.post("/v1/run-plane/runs/{run_id}/tools/{name}/invoke")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_invoke(run_id: str, name: str, body: InvokeBody, request: Request,
                  x_boss_replica: Optional[str] = Header(default=None),
                  x_boss_job: Optional[str] = Header(default=None),
                  x_boss_run_token: Optional[str] = Header(default=None),
                  x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.tool_invoke, name=name,
                         call=body.call, approved=body.approved)

    # ── the console ───────────────────────────────────────────────────────────────────────

    @router.get("/v1/fleet/hosts")
    @permit(P.FLEET_READ, scope=Scope.org())
    def list_hosts(request: Request) -> dict[str, Any]:
        with begin(engine) as connection:
            return {"hosts": _fleet.list_hosts(connection, _org(request, connection))}

    @router.get("/v1/fleet/hosts/{agent_host_id}")
    @permit(P.FLEET_READ, scope=Scope.org())
    def get_host(agent_host_id: str, request: Request) -> dict[str, Any]:
        with begin(engine) as connection:
            found = _fleet.get_host(connection, _org(request, connection), agent_host_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such agent host.")
        return found

    @router.post("/v1/fleet/join-tokens", status_code=201)
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def issue_join_token(body: JoinTokenBody, request: Request) -> dict[str, Any]:
        """A one-time join token: in this answer once, never again."""
        try:
            with begin(engine) as connection:
                return _fleet.issue_join_token(connection, org_id=_org(request, connection),
                                               actor=_who(request, body.actor), pool=body.pool,
                                               labels=body.labels, hours=body.hours)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.delete("/v1/fleet/join-tokens/{token_id}")
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def revoke_join_token(token_id: str, request: Request) -> dict[str, Any]:
        with begin(engine) as connection:
            if not _fleet.revoke_join_token(connection, org_id=_org(request, connection),
                                            token_id=token_id, actor=_who(request, None)):
                raise HTTPException(status_code=404, detail="no such unused join token.")
        return {"revoked": token_id}

    @router.post("/v1/fleet/hosts/{agent_host_id}/revoke")
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def revoke_host(agent_host_id: str, body: RevokeBody, request: Request) -> dict[str, Any]:
        """Every call the host makes from now on, and its next renewal, is refused."""
        try:
            with begin(engine) as connection:
                out = _fleet.revoke(connection, org_id=_org(request, connection),
                                    host_id=agent_host_id, actor=_who(request, body.actor),
                                    reason=body.reason)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        out["reconcile"] = fleet_scheduler.reconcile(engine, _settings())
        return out

    def _cordon(agent_host_id: str, request: Request, body: ActorBody, cordoned: bool) -> dict:
        try:
            with begin(engine) as connection:
                out = _fleet.set_cordon(connection, org_id=_org(request, connection),
                                        host_id=agent_host_id, actor=_who(request, body.actor),
                                        cordoned=cordoned)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        fleet_scheduler.reconcile(engine, _settings())
        return out

    @router.post("/v1/fleet/hosts/{agent_host_id}/cordon")
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def cordon(agent_host_id: str, body: ActorBody, request: Request) -> dict[str, Any]:
        return _cordon(agent_host_id, request, body, True)

    @router.post("/v1/fleet/hosts/{agent_host_id}/uncordon")
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def uncordon(agent_host_id: str, body: ActorBody, request: Request) -> dict[str, Any]:
        return _cordon(agent_host_id, request, body, False)

    @router.post("/v1/fleet/reconcile")
    @permit(P.FLEET_MANAGE, scope=Scope.org())
    def reconcile(request: Request) -> dict[str, Any]:
        """One scheduler pass now (leader-safe: a pass already running answers `busy`)."""
        with begin(engine) as connection:
            org = _org(request, connection)
        return fleet_scheduler.reconcile(engine, _settings(), org_id=org)

    @router.put("/v1/agents/{role_key}/deployments/{environment}/runtime")
    @permit(P.AGENT_DEPLOY, scope=Scope.project(body="project_id"))
    def set_runtime(role_key: str, environment: str, body: RuntimeBody) -> dict[str, Any]:
        """Where a deployment's runs execute: `local` (the worker, the default) or `microvm`
        (replicas on enrolled hosts). Runs already admitted keep theirs."""
        try:
            with begin(engine) as connection:
                actor = (require_write_actor(connection, body.project_id, body.actor,
                                             "workflow_manage")
                         if require_write_actor is not None else str(body.actor or "operator"))
                out = agent_deployments.set_runtime(
                    connection, body.project_id, role_key, environment, actor=actor,
                    runtime=body.runtime, vm_mode=body.vm_mode, replicas=body.replicas,
                    resources=body.resources, image_digest=body.image_digest,
                    placement=body.placement, settings=_settings())
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        out["reconcile"] = fleet_scheduler.reconcile(engine, _settings())
        with begin(engine) as connection:
            out["placement_state"] = (agent_deployments.by_id(connection, out["id"]) or {}).get(
                "placement_state")
        return out

    return router


__all__ = ["build_fleet_router"]
