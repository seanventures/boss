"""Agent hosts, the fleet and the run plane over HTTP (P8a, BOSSAPP-1063;
boss-agent-hosts-lld.md §3.2, §3.13.3). ONE router for the phase.

  HOSTS (boss-hostd → control plane; authenticated as the host, `fleet:host`, system only)
      POST /v1/hosts/enrol                               public: a one-time join token + CSR
      POST /v1/hosts/{agent_host_id}/renew | /heartbeat | /observed | /guest-calls | /events
      PUT  /v1/hosts/{agent_host_id}/replicas               the full replica map, after a resync
      GET  /v1/hosts/{agent_host_id}/desired?since=&wait=  level-based watch (long-poll)
  RUN PLANE (boss-hostd on a VM's behalf; `fleet:host`; every run call fenced)
      POST /v1/run-plane/hosts/next                         ONE long-poll per host (§3.13.0)
      POST /v1/run-plane/admit
      GET  /v1/run-plane/runs/{run_id}                      the run package (never its token)
      POST /v1/run-plane/runs/{run_id}/lease                heartbeat | settle | fail | hand_back
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
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
    #: What the host can do (P8b S13: "host_tokens" — it holds per-run DPoP keys).
    capabilities: list[str] = Field(default_factory=list)


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
    capabilities: list[str] = Field(default_factory=list)


class ObservedBody(BaseModel):
    replicas: list[dict[str, Any]] = Field(default_factory=list)
    observed_generation: Optional[int] = None
    state_gen: Optional[int] = None


class GuestCallsBody(BaseModel):
    calls: list[dict[str, Any]] = Field(default_factory=list)


class HostEventsBody(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class HostsNextBody(BaseModel):
    """The whole poll: no replica list (BOSS keeps its own model, §3.13.0 REVISION)."""
    wait_ms: int = Field(default=0, ge=0, le=30000)
    state_gen: Optional[int] = None
    ttl: Optional[int] = None


class ReplicaMapBody(BaseModel):
    state_gen: int = Field(ge=0)
    replicas: list[dict[str, Any]] = Field(default_factory=list)


class AdmitBody(BaseModel):
    replica_id: Optional[str] = None
    caller: str = ""
    input: Any = None
    thread_id: Optional[str] = None
    double_text: Optional[str] = None
    traceparent: Optional[str] = None
    ttl: Optional[int] = None


class LeaseBody(BaseModel):
    action: str = ""
    hand_back: bool = False
    ttl: Optional[int] = None
    outcome: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class RunTokensBody(BaseModel):
    """S13: the host's DPoP key thumbprint for this run and the destinations it wants tokens
    for (HOST_API.md §4.8). Never a key, never a token: both go the other way."""
    jkt: str = Field(min_length=16, max_length=128)
    audiences: Optional[list[str]] = None
    scope: Optional[list[str]] = None


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
                  fn: Callable[..., dict], *, one_statement: bool = False, **kw: Any) -> dict:
        """A fenced run call in its own transaction — or, for the hot writes whose fence and
        write are ONE statement (run_plane._LEASE_HELD), in autocommit: no BEGIN/COMMIT trips."""
        host = _host(request)
        _run_plane_delegation(request, host.host_id, run_id)
        try:
            if one_statement:
                with engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT") as connection:
                    return fn(connection, _settings(), host.host_id, run_id, lease, **kw)
            with begin(engine) as connection:
                return fn(connection, _settings(), host.host_id, run_id, lease, **kw)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    _ASSERTION_HEADER = "boss-run-assertion"

    def _via_guest_gateway(request: Request) -> bool:
        """The call was forwarded for a VM by its host's guest gateway, which sets the run
        assertion on every forward (HOST_API.md §4.2 step 5); the host's own calls carry none."""
        return bool((request.headers.get(_ASSERTION_HEADER) or "").strip())

    def _run_plane_delegation(request: Request, host_id: str, run_id: str) -> None:
        """S13: a run call forwarded for a VM carries the run's boss:run_plane token and the
        host's proof; required for a host that advertised `host_tokens` (sticky). The host's
        own calls (hand back, minting) are the host's, authenticated by its proof alone."""
        if not _via_guest_gateway(request):
            return
        from boss_engine.identity import delegation, host_delegation

        refused: Optional[str] = None
        with begin(engine) as connection:   # committed: a spent jti stays spent
            try:
                host_delegation.check(
                    connection, dict(request.headers), audience=delegation.AUD_RUN_PLANE,
                    run_id=run_id, method=request.method, url=str(request.url).split("?", 1)[0],
                    required=(not host_delegation.presented(request.headers)
                              and _fleet.host_tokens_required(connection, host_id)))
            except host_delegation.DelegationRefused as exc:
                refused = exc.reason
        if refused is not None:
            raise HTTPException(status_code=401, detail={
                "error": f"run_token_{refused}",
                "message": "the run's delegated run-plane token was refused"})

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
                                   capacity=body.capacity, labels=body.labels,
                                   capabilities=body.capabilities)
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
    async def desired(agent_host_id: str, request: Request, since: int = 0,
                      wait: float = 0.0) -> dict[str, Any]:
        """Level-based: the whole desired state once the generation is past `since`. An async
        wait: a long-poll holds no worker thread and queries nothing while it waits."""
        await run_in_threadpool(_host, request, agent_host_id)
        try:
            return await _fleet.watch(engine, agent_host_id, since=since, wait=wait)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.post("/v1/hosts/{agent_host_id}/observed")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def observed(agent_host_id: str, body: ObservedBody, request: Request) -> dict[str, Any]:
        """Every replica state change, pushed at once, with the host's new state_gen."""
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                out = _fleet.observed(connection, host, body.model_dump())
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        run_plane.wake_host(host.host_id)
        return out

    @router.put("/v1/hosts/{agent_host_id}/replicas")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def put_replicas(agent_host_id: str, body: ReplicaMapBody,
                     request: Request) -> dict[str, Any]:
        """The host's FULL replica map, sent once after a {resync: true}: BOSS stores it and the
        generation; a full map is never sent otherwise."""
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                out = _fleet.put_replicas(connection, host, state_gen=body.state_gen,
                                          replicas=body.replicas)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise
        run_plane.wake_host(host.host_id)
        return out

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

    @router.post("/v1/hosts/{agent_host_id}/events")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def host_events(agent_host_id: str, body: HostEventsBody, request: Request) -> dict[str, Any]:
        """The watchdog's and the guest gateway's detail (HOST_API.md §2.7) as host_events."""
        host = _host(request, agent_host_id)
        try:
            with begin(engine) as connection:
                return _fleet.host_events_ingest(connection, host, body.events)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    # ── the run plane ─────────────────────────────────────────────────────────────────────

    def _replica(header: Optional[str], claimed: Optional[str]) -> str:
        """WHICH REPLICA IS CALLING is the host's word, never the guest's: boss-hostd's guest
        gateway sets X-Boss-Replica from the vsock socket the call arrived on (HOST_API.md
        §4.2). A body `replica_id` that names another replica is refused."""
        replica = str(header or "").strip()
        if not replica:
            raise HTTPException(status_code=400, detail={
                "error": "replica_header_required",
                "message": "the host names the replica in X-Boss-Replica"})
        if claimed and claimed != replica:
            raise HTTPException(status_code=403, detail={
                "error": "replica_mismatch",
                "message": "a guest cannot speak for another replica"})
        return replica

    @router.post("/v1/run-plane/hosts/next")
    @permit(P.FLEET_HOST, scope=Scope.org())
    async def rp_hosts_next(body: HostsNextBody, request: Request,
                            x_boss_replica: Optional[str] = Header(default=None)
                            ) -> dict[str, Any]:
        """ONE outstanding work request per HOST (§3.13.0): {wait_ms, state_gen}, no replica
        list. A stale state_gen → {resync: true} (PUT …/replicas once). Otherwise runs for the
        host's serving replicas (BOSS's own slot count), each leased to one replica at epoch + 1
        — or an async wait (≤ wait_ms) for this host's deployments, then a second claim. A call
        that carries X-Boss-Replica came through a guest gateway: a VM never polls BOSS."""
        if x_boss_replica:
            raise HTTPException(status_code=403, detail={
                "error": "host_only", "message": "hosts/next is the host's own call; a VM "
                "never polls for work (HOST_API.md §2.8)"})
        host = await run_in_threadpool(_host, request)
        try:
            return await run_plane.host_next(engine, _settings(), host.host_id,
                                             state_gen=body.state_gen, wait_ms=body.wait_ms,
                                             ttl=body.ttl)
        except Exception as error:  # noqa: BLE001
            _fleet_fail(error)
            raise

    @router.get("/v1/run-plane/runs/{run_id}")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_run_package(run_id: str, request: Request,
                       x_boss_replica: Optional[str] = Header(default=None),
                       x_boss_job: Optional[str] = Header(default=None),
                       x_boss_run_token: Optional[str] = Header(default=None),
                       x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        """The run the VM just accepted: what it drives the kernel from (never its token)."""
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.run_package)

    @router.post("/v1/run-plane/admit")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_admit(body: AdmitBody, request: Request,
                 x_boss_replica: Optional[str] = Header(default=None)) -> dict[str, Any]:
        """PDP agent:invoke on the forwarded caller + thread_runs.admit + the lease to this
        replica, in one transaction."""
        host = _host(request)
        replica = _replica(x_boss_replica, body.replica_id)
        try:
            with begin(engine) as connection:
                return run_plane.admit(connection, _settings(), host.host_id, replica,
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
        if body.hand_back or body.action == "hand_back":
            # The VM refused the run or died before accepting it: re-queued now, epoch + 1 next.
            out = _run_call(request, run_id, lease, run_plane.hand_back)
            run_plane.wake_host(_host(request).host_id)
            return out
        if body.action == "heartbeat":
            return _run_call(request, run_id, lease, run_plane.heartbeat, ttl=body.ttl,
                             one_statement=True)
        if body.action == "settle":
            out = _run_call(request, run_id, lease, run_plane.settle, outcome=body.outcome)
            run_plane.after_commit(engine, run_id)
            run_plane.wake_host(_host(request).host_id)
            return out
        if body.action == "fail":
            out = _run_call(request, run_id, lease, run_plane.fail, error=body.error)
            run_plane.after_commit(engine, run_id)
            run_plane.wake_host(_host(request).host_id)
            return out
        raise HTTPException(status_code=400, detail={
            "error": "bad_action", "message": "action is heartbeat, settle, hand_back or fail"})

    @router.post("/v1/run-plane/runs/{run_id}/tokens")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def rp_run_tokens(run_id: str, body: RunTokensBody, request: Request,
                      x_boss_replica: Optional[str] = Header(default=None),
                      x_boss_job: Optional[str] = Header(default=None),
                      x_boss_run_token: Optional[str] = Header(default=None),
                      x_boss_fence_epoch: Optional[str] = Header(default=None)) -> dict[str, Any]:
        """S13 (BOSSAPP-1074): the run's delegated, per-destination, DPoP-bound tokens for the
        HOST's key (body.jkt). Fenced like every run call; the host keeps the answer in memory
        and attaches one token (plus a proof) per forward. Called again with a new jkt, it is
        the host's rotation at two thirds of the TTL."""
        if _via_guest_gateway(request):
            # A VM must never mint tokens for a key it holds (P8b-T5): the host's guest gateway
            # denies this path, and this refuses it again should one ever be forwarded.
            raise HTTPException(status_code=403, detail={
                "error": "host_only", "message": "run tokens are the host's own call; a VM "
                "never mints them (HOST_API.md §4.8)"})
        lease = _lease(x_boss_replica, x_boss_job, x_boss_run_token, x_boss_fence_epoch)
        return _run_call(request, run_id, lease, run_plane.host_tokens, jkt=body.jkt,
                         audiences=body.audiences, scope=body.scope)


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
        return _run_call(request, run_id, lease, run_plane.put_checkpoint, row=body.row,
                         one_statement=True)

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
        return _run_call(request, run_id, lease, run_plane.put_write, row=body.row,
                         one_statement=True)

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
        run_plane.wake_desired([agent_host_id])
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


    # ── the MCP/tool gateway, host-facing (P8b S12; HOST_API.md §4.7) ─────────────────────

    from boss_engine.services import mcp_gateway as _mcpgw

    _MAX_MCP_BODY = 8 * 1024 * 1024

    def _mcp_refusal(status: int, reason: str, message: str = "") -> Response:
        body = {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32001, "message": f"boss mcp gateway: {message or reason}", "data": {
                "reason": reason}}}
        return JSONResponse(status_code=status, content=body, headers={"connection": "close"})

    @router.post("/mcp/{server}")
    @permit(P.FLEET_HOST, scope=Scope.org())
    async def mcp_gateway_rpc(server: str, request: Request) -> Response:
        """A guest's MCP call, forwarded by its boss-hostd (HOST_API.md §4.7): the host's
        BossHost proof authenticates; `X-Boss-Handle` is the guest's `boss_mcpgw_` handle moved
        out of its credential header; `X-Boss-Vm/Run/Fence-Epoch/Deployment` are the host's
        word, which the handle's row must agree with. The run's allowlist, the policy engine,
        parking and receipts are the loopback gateway's own (mcp_gateway.McpGateway.handle).
        A `tools/call` the client accepts as SSE streams keepalives while a person decides."""
        import json as _json
        import time as _time

        started = _time.monotonic()
        host = await run_in_threadpool(_host, request)
        gateway = _mcpgw.get()
        lane = gateway.host_lane
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            caller = _mcpgw.HostCaller.from_headers(headers, host_id=host.host_id,
                                                    org_id=host.org_id or None)
        except _mcpgw.Refused as exc:
            return _mcp_refusal(exc.status, exc.reason, str(exc))
        raw = _mcpgw.presented_handle(headers)
        fp = _mcpgw.fingerprint(raw) if raw else None
        server_key = server.strip("/") or _mcpgw.RUN_SERVER
        url = str(request.url).split("?", 1)[0]

        def refuse(status: int, reason: str, message: str = "", grant: Any = None) -> Response:
            lane.record(caller, grant, server_key=server_key, rpc_method="", status=status,
                        verdict="deny", rejected_by=reason, started=started, handle_fp=fp)
            return _mcp_refusal(status, reason, message)

        def _resolve() -> Any:
            # A refusal still COMMITS what it wrote: a replay's revocation and its
            # host_events row, a spent DPoP jti. Raising out of the transaction would undo them.
            refused: Optional[_mcpgw.Refused] = None
            grant = None
            with begin(engine) as connection:
                try:
                    grant = lane.resolve(connection, caller, raw)
                    lane.check_delegation(connection, headers, run_id=grant.run_id or "",
                                          method="POST", url=url, host_id=host.host_id)
                except _mcpgw.Refused as exc:
                    refused = exc
            if refused is not None:
                raise refused
            return grant

        try:
            grant = await run_in_threadpool(_resolve)
        except _mcpgw.Refused as exc:
            return refuse(exc.status, exc.reason, str(exc))
        if server_key != _mcpgw.RUN_SERVER and server_key not in grant.servers:
            # Outside the run's allowlist: refused before any upstream lookup (403, typed).
            lane.record(caller, grant, server_key=server_key, rpc_method="", status=403,
                        verdict="deny", rejected_by="server_not_granted", started=started,
                        handle_fp=fp)
            return JSONResponse(status_code=403, content=_mcpgw._rpc_result(None, _mcpgw.tool_error(
                "denied_by_policy", f"no server '{server_key}' on this run: it is not in the "
                "agent's grant", server=server_key)), headers={"connection": "close"})
        raw_body = await request.body()
        if len(raw_body) > _MAX_MCP_BODY:
            return refuse(413, "body_too_large", grant=grant)
        try:
            message = _json.loads(raw_body or b"null")
        except ValueError:
            return JSONResponse(status_code=400, content=_mcpgw._rpc_error(None, -32700,
                                                                            "parse error"))
        rpc_method = str(message.get("method") or "") if isinstance(message, dict) else "batch"

        def done(status: int, verdict: str = "allow", rejected: Optional[str] = None,
                 size: int = 0, answer: Any = None) -> None:
            if answer is not None and verdict == "allow":
                verdict, rejected = _mcpgw.answer_verdict(answer)
            lane.record(caller, grant, server_key=server_key, rpc_method=rpc_method,
                        status=status, verdict=verdict, rejected_by=rejected, started=started,
                        handle_fp=fp, bytes_in=len(raw_body), bytes_out=size)

        if isinstance(message, list):
            answers = await run_in_threadpool(lambda: [
                a for a in (gateway.handle(grant, server_key, m) for m in message[:50]
                            if isinstance(m, dict)) if a is not None])
            if not answers:
                done(202)
                return Response(status_code=202)
            out = _json.dumps(answers).encode()
            done(200, size=len(out), answer=answers)
            return Response(out, media_type="application/json")
        if not isinstance(message, dict):
            done(400, "deny", "invalid_request")
            return JSONResponse(status_code=400, content=_mcpgw._rpc_error(None, -32600,
                                                                            "invalid request"))
        if "id" not in message:
            await run_in_threadpool(gateway.handle, grant, server_key, message)
            done(202)
            return Response(status_code=202)
        streams = "text/event-stream" in (headers.get("accept") or "")
        if rpc_method == "tools/call" and streams:
            # A CALL THAT MAY PARK is answered as a stream: keepalives while a person decides,
            # then the one result event. The handler runs in a worker thread; the response
            # drains its queue, and a client that hangs up stops the keepalives.
            sink = _mcpgw.QueueSink()
            final: dict = {}

            def work() -> None:
                try:
                    answer = gateway.handle(grant, server_key, message, sink)
                except Exception as exc:  # noqa: BLE001
                    import logging

                    logging.getLogger("boss.mcp_gateway").warning(
                        "mcp gateway call failed: %s", type(exc).__name__, exc_info=True)
                    answer = _mcpgw._rpc_error(message.get("id"), -32603, "internal error")
                final["answer"] = answer
                sink.finish(answer)

            import threading

            threading.Thread(target=work, daemon=True, name="boss-mcp-gateway-host").start()

            async def relay():
                import anyio

                sent = 0
                try:
                    while True:
                        chunk = await anyio.to_thread.run_sync(sink.q.get)
                        if chunk is None:
                            break
                        sent += len(chunk)
                        yield chunk
                finally:
                    sink.gone.set()
                    if "answer" in final:
                        done(200, size=sent, answer=final["answer"])
                    else:
                        done(200, "allow", "client_disconnected", size=sent)

            return StreamingResponse(relay(), media_type="text/event-stream",
                                     headers={"cache-control": "no-cache", "connection": "close"})
        try:
            answer = await run_in_threadpool(gateway.handle, grant, server_key, message, None)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger("boss.mcp_gateway").warning("mcp gateway request failed: %s",
                                                           type(exc).__name__, exc_info=True)
            answer = _mcpgw._rpc_error(message.get("id"), -32603, "internal error")
        out = _json.dumps(answer).encode()
        done(200, size=len(out), answer=answer)
        return Response(out, media_type="application/json")

    @router.get("/mcp/{server}")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def mcp_gateway_get(server: str, request: Request) -> Response:
        """No server-initiated stream: every answer rides the POST that asked for it."""
        _host(request)
        return Response(status_code=405, headers={"allow": "POST"})

    @router.delete("/mcp/{server}")
    @permit(P.FLEET_HOST, scope=Scope.org())
    def mcp_gateway_delete(server: str, request: Request) -> Response:
        """Sessions are not kept here; there is nothing to end."""
        _host(request)
        return Response(status_code=405, headers={"allow": "POST"})

    return router


__all__ = ["build_fleet_router"]
