"""MT-10 HTTP endpoints for capabilities, signed policy, reviewer, grants, audit & host leases."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public
from boss_engine.services import metatron_server

from .. import identity_guard
from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["metatron-v1"])


class ReviewCallBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str = Field(min_length=1, max_length=200)
    argv_redacted: Any = Field(default_factory=list)
    host: Optional[str] = None
    domain: Optional[str] = None
    risk_class: Optional[str] = None


class ApprovalReviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    turn_id: Optional[str] = None
    call: ReviewCallBody
    mode: str = "act-on-my-behalf"
    actor: Optional[str] = None


class GrantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str = Field(min_length=1, max_length=200)
    scope: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    max_risk_class: Optional[str] = None
    ttl_s: int = Field(default=3600, ge=1, le=86400)
    actor: Optional[str] = None


class AuditIngestRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    project_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    events: list[dict[str, Any]] = Field(default_factory=list)
    actor: Optional[str] = None


class CredentialLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    purpose: str = Field(min_length=1, max_length=400)
    ttl_s: int = Field(default=60, ge=1, le=300)
    session_id: Optional[str] = None
    actor: Optional[str] = None


def _ctx(request: Request) -> Any:
    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached
    try:
        return identity_guard.request_auth(request)
    except Exception:  # noqa: BLE001
        return None


def _actor(request: Request, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    ctx = _ctx(request)
    if ctx is not None:
        return str(identity_guard.actor_for(ctx) or ctx.principal.id)
    return "executive_console"


@router.get("/v1/metatron/capabilities")
@authenticated()
def get_metatron_capabilities(request: Request) -> dict[str, Any]:
    return metatron_server.capabilities_document(str(request.app.version))


@router.get("/v1/metatron/policy")
@permit(P.POLICY_READ, scope=Scope.project(query="project_id"))
def get_metatron_policy(project_id: str, request: Request) -> Response:
    if not project_id.strip():
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_project_id", "message": "project_id is required.", "retryable": False}},
        )
    try:
        with get_engine().connect() as c:
            bundle_payload, etag = metatron_server.build_and_sign_policy_bundle(c, project_id)
    except metatron_server.MetatronServerError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    if_none_match = request.headers.get("if-none-match")
    if metatron_server.etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=60"})

    return JSONResponse(
        status_code=200,
        content=bundle_payload,
        headers={"ETag": etag, "Cache-Control": "private, max-age=60"},
    )


@router.get("/v1/metatron/policy/keys")
@public(reason=PublicReason.JWKS, verifier=None)
def get_metatron_policy_keys() -> dict[str, Any]:
    with get_engine().connect() as c:
        return metatron_server.policy_public_jwks(c)


@router.post("/v1/metatron/approvals/review")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def post_metatron_approval_review(payload: ApprovalReviewRequest, request: Request) -> JSONResponse:
    ctx = _ctx(request)
    actor = _actor(request, payload.actor)
    principal_id = str(ctx.principal.id) if ctx is not None else actor
    org_id = ctx.principal.org_id if ctx is not None else None
    try:
        result = metatron_server.review_approval(
            get_engine(),
            get_settings(),
            project_id=payload.project_id,
            principal_id=principal_id,
            org_id=org_id,
            actor=actor,
            session_id=payload.session_id,
            turn_id=payload.turn_id,
            call=payload.call.model_dump(),
            mode=payload.mode,
        )
    except metatron_server.MetatronServerError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())
    return JSONResponse(status_code=200, content=result)


@router.post("/v1/metatron/grants", status_code=201)
@permit(P.AUTOPILOT_OPERATE, scope=Scope.project(body="project_id"))
def post_metatron_grant(payload: GrantCreateRequest, request: Request) -> JSONResponse:
    ctx = _ctx(request)
    actor = _actor(request, payload.actor)
    principal_id = str(ctx.principal.id) if ctx is not None else actor
    try:
        with begin(get_engine()) as c:
            from boss_engine.services import tool_policy_engine

            org_id = (ctx.principal.org_id if ctx is not None else None) or tool_policy_engine.org_of(
                c, payload.project_id
            )
            if not org_id:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "project_not_found", "message": "Project not found.", "retryable": False}},
                )
            grant = metatron_server.create_grant(
                c,
                org_id=str(org_id),
                project_id=payload.project_id,
                principal_id=principal_id,
                actor=actor,
                scope=payload.scope,
                allowed_tools=payload.allowed_tools,
                max_risk_class=payload.max_risk_class,
                ttl_s=payload.ttl_s,
            )
    except metatron_server.MetatronServerError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())
    return JSONResponse(status_code=201, content=grant)


@router.delete("/v1/metatron/grants/{grant_id}")
@authenticated(self_only=True)
def delete_metatron_grant(grant_id: str, request: Request) -> JSONResponse:
    ctx = _ctx(request)
    principal_id = str(ctx.principal.id) if ctx is not None else _actor(request)
    with begin(get_engine()) as c:
        revoked = metatron_server.mark_grant_revoked(c, grant_id=grant_id, principal_id=principal_id)
    if not revoked:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "grant_not_found", "message": "Grant was not found.", "retryable": False}},
        )
    return JSONResponse(status_code=200, content={"revoked": grant_id})


@router.post("/v1/metatron/audit")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def post_metatron_audit(payload: AuditIngestRequest, request: Request) -> JSONResponse:
    ctx = _ctx(request)
    actor = _actor(request, payload.actor)
    principal_id = str(ctx.principal.id) if ctx is not None else actor
    with begin(get_engine()) as c:
        from boss_engine.services import tool_policy_engine

        org_id = (ctx.principal.org_id if ctx is not None else None) or tool_policy_engine.org_of(
            c, payload.project_id
        )
        if not org_id:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "project_not_found", "message": "Project not found.", "retryable": False}},
            )
        out = metatron_server.ingest_audit_batch(
            c,
            org_id=str(org_id),
            project_id=payload.project_id,
            principal_id=principal_id,
            actor=actor,
            session_id=payload.session_id,
            events=payload.events,
        )
    return JSONResponse(status_code=200, content=out)


@router.get("/v1/metatron/hosts")
@permit(P.HOST_READ, scope=Scope.project(query="project_id"))
def get_metatron_hosts(project_id: str) -> dict[str, Any]:
    with get_engine().connect() as c:
        hosts = metatron_server.list_metatron_hosts(c, project_id)
    return {"project_id": project_id, "hosts": hosts}


@router.post("/v1/metatron/hosts/{host_id}/credential-lease")
@permit(P.HOST_EXEC, scope=Scope.of("deployment_host", path="host_id"))
def post_metatron_host_credential_lease(
    host_id: str, payload: CredentialLeaseRequest, request: Request
) -> JSONResponse:
    ctx = _ctx(request)
    actor = _actor(request, payload.actor)
    principal_id = str(ctx.principal.id) if ctx is not None else actor
    try:
        with begin(get_engine()) as c:
            lease = metatron_server.issue_host_credential_lease(
                c,
                get_settings(),
                host_id=host_id,
                principal_id=principal_id,
                actor=actor,
                purpose=payload.purpose,
                ttl_s=payload.ttl_s,
                session_id=payload.session_id,
            )
    except metatron_server.MetatronServerError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers={"Cache-Control": "no-store"})
    return JSONResponse(status_code=200, content=lease, headers={"Cache-Control": "no-store"})
