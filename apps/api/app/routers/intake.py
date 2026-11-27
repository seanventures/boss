"""EVENT INTAKE over HTTP (P9a-1, BOSSAPP-1093; boss-eventing-lld.md §2.2, §10.1, §11;
wireframes E1 Sources, E5 Stream, E6 Dead letters). The console work is a later ticket; these
are the shapes it will read.

  THE FRONT DOOR (public, signed by the source itself)
  POST    /v1/intake/{kind}/{connection}            verify → insert → commit → the source's 2xx
  OPTIONS /v1/intake/{kind}/{connection}            CloudEvents abuse-protection handshake

  CONNECTIONS (E1)                                   integration:read / integration:manage
  GET     /v1/intake/catalog                         the adapters: label, modes, fields, hosts
  GET     /v1/intake/connections?project_id=         rows with health, last hour, backlog, rules
  POST    /v1/intake/connections                     create; a minted secret is in the answer ONCE
  GET     /v1/intake/connections/{connection_id}
  PATCH   /v1/intake/connections/{connection_id}     name, config, limits, source-issued secrets
  DELETE  /v1/intake/connections/{connection_id}
  POST    /v1/intake/connections/{connection_id}/test            secrets resolve, inbox stats
  POST    /v1/intake/connections/{connection_id}/test-message    post BOSS's own test message (adapter send)
  GET     /v1/intake/connections/{connection_id}/test-message    did a person answer that post
  POST    /v1/intake/connections/{connection_id}/rotate-secret   new secret, shown ONCE
  POST    /v1/intake/connections/{connection_id}/pause | /resume
  PUT/DELETE /v1/intake/connections/{connection_id}/sso-attestation   identity_provider:manage

  PEOPLE LINKS (E7)
  GET     /v1/intake/connections/{connection_id}/people                  integration:read
  POST    /v1/intake/connections/{connection_id}/people/prompts          integration:manage
  POST    /v1/intake/connections/{connection_id}/people/{link_id}/revoke integration:manage

  RULES
  GET     /v1/intake/rules?project_id=&source=&agent=&enabled=   agent:read

  EVENTS (E5) and DEAD LETTERS (E6)
  GET     /v1/intake/events?project_id=&status=&connection_id=&q=&hours=&limit=
  GET     /v1/intake/events/{intake_event_id}        envelope, guards, raw, the fires it caused
  GET     /v1/intake/dead-letters?project_id=
  POST    /v1/intake/events/replay                   {project_id, intake_event_ids, mode, reason}

The front door does blocking DB work, so it runs in the threadpool (never on the event loop,
LLD finding F9) and reads a bounded body (413 beyond 1 MB, as the chat route does).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from boss_engine.connectors.intake import REGISTRY
from boss_engine.connectors.intake.contract import MAX_BODY_BYTES, RawRequest
from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, permit, public
from boss_engine.services import agent_triggers, event_intake


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class ConnectionCreateBody(ProjectBody):
    kind: str
    name: str = ""
    mode: str = "push"
    config: dict[str, Any] = Field(default_factory=dict)
    #: secret field values (the connection keeps only references); a missing `signing_secret`
    #: is minted and shown once in the answer
    secrets: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)


class ConnectionPatchBody(BaseModel):
    actor: Optional[str] = None
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    secrets: Optional[dict[str, str]] = None
    limits: Optional[dict[str, int]] = None


class ActorBody(BaseModel):
    actor: Optional[str] = None
    reason: str = ""


class RotateBody(BaseModel):
    actor: Optional[str] = None
    field: Optional[str] = None


class AttestationBody(BaseModel):
    """§6.3: which BOSS identity provider the source's workspace signs in through, and which of
    its verified domains may auto-link (default all). Who attested and when are stamped by BOSS."""
    actor: Optional[str] = None
    identity_provider_id: str
    domains: Optional[list[str]] = Field(default=None, max_length=50)


class PromptBody(BaseModel):
    actor: Optional[str] = None
    external_user_id: Optional[str] = None
    resend: bool = False


class ReplayBody(ProjectBody):
    intake_event_ids: list[str] = Field(default_factory=list, max_length=500)
    mode: str = "reprocess"
    reason: str = ""


async def _read_bounded(request: Request) -> tuple[bytes, bool]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return b"", True
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return b"", True
        chunks.append(chunk)
    return b"".join(chunks), False


def _fail(error: Exception):
    if isinstance(error, event_intake.IntakeServiceError):
        raise HTTPException(status_code=error.status,
                            detail={"error": error.code, "message": str(error)}) from error
    raise error


def people_visible(request: Request, db: Any = None) -> bool:
    """Does the caller hold user:read (the directory permission GET /v1/identity/users needs:
    Owner, Admin, Auditor)? Asked without recording a denial: a missing one withholds the BOSS
    person behind each chat link, it refuses nothing. No identity on the request: withheld."""
    try:
        from .. import identity_guard
        from boss_engine.identity import authz
        from boss_engine.identity.scope import ResolvedScope

        ctx = identity_guard.request_auth(request)
        if ctx is None:
            return False
        held = authz.effective_permissions(
            ctx, ResolvedScope.org_level(ctx.principal.org_id), db=db).permissions
        return "user:read" in held
    except Exception:  # noqa: BLE001 — fail closed: the names stay withheld
        return False


def build_intake_router(*, engine, settings: Callable[[], Any],
                        require_write_actor: Callable[..., str],
                        require_reader: Callable[..., None]) -> APIRouter:
    router = APIRouter(tags=["event-intake"])

    def _public_base(request: Request) -> str:
        try:
            from .ticket_sync import _public_base as base_of

            return base_of(request, "so the source can reach BOSS")
        except HTTPException:
            return ""

    def _with_url(request: Request, out: dict) -> dict:
        base = _public_base(request)
        if base and out.get("url_path"):
            out["url"] = base + out["url_path"]
        return out

    def _connection_project(db, connection_id: str) -> str:
        row = event_intake.get_connection(db, connection_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found",
                                                        "message": "No such intake connection."})
        return str(row["project_id"])

    # ── connections (E1) ─────────────────────────────────────────────────────────────────────

    @router.get("/v1/intake/catalog")
    @permit(P.INTEGRATION_READ, scope=Scope.org())
    def intake_catalog() -> dict:
        """The sources BOSS can connect: manifests (label, modes, capabilities, auth fields,
        official hosts). Secret fields are flagged; the console renders them as password inputs."""
        return {"sources": [m.to_dict() for m in REGISTRY.manifests()]}

    @router.get("/v1/intake/connections")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_intake_connections(project_id: str, request: Request) -> dict:
        with begin(engine) as db:
            require_reader(db, project_id)
            rows = event_intake.list_connections(db, project_id)
        return {"connections": [_with_url(request, r) for r in rows]}

    @router.post("/v1/intake/connections", status_code=201)
    @permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
    def create_intake_connection(body: ConnectionCreateBody, request: Request) -> dict:
        """Connect a source. The URL to paste into the source and (for sources BOSS signs for) the
        minted secret are in THIS answer only."""
        with begin(engine) as db:
            actor = require_write_actor(db, body.project_id, body.actor, "integration_manage")
            try:
                out = event_intake.create_connection(
                    db, project_id=body.project_id, kind=body.kind, actor=actor, name=body.name,
                    mode=body.mode, config=body.config, secrets=body.secrets, limits=body.limits)
            except Exception as error:  # noqa: BLE001
                _fail(error)
        return _with_url(request, out)

    @router.get("/v1/intake/connections/{connection_id}")
    @permit(P.INTEGRATION_READ, scope=Scope.of("intake_connection", path="connection_id"))
    def get_intake_connection(connection_id: str, request: Request) -> dict:
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_reader(db, project_id)
            row = event_intake.get_connection(db, connection_id, project_id=project_id)
            rows = event_intake.list_connections(db, project_id)
        found = next((r for r in rows if r["id"] == connection_id), None)
        if row is None or found is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return _with_url(request, found)

    @router.patch("/v1/intake/connections/{connection_id}")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def patch_intake_connection(connection_id: str, body: ConnectionPatchBody,
                                request: Request) -> dict:
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            actor = require_write_actor(db, project_id, body.actor, "integration_manage")
            try:
                out = event_intake.update_connection(
                    db, project_id=project_id, connection_id=connection_id, actor=actor,
                    name=body.name, config=body.config, limits=body.limits, secrets=body.secrets)
            except Exception as error:  # noqa: BLE001
                _fail(error)
        return _with_url(request, out)

    @router.delete("/v1/intake/connections/{connection_id}")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def delete_intake_connection(connection_id: str, actor: Optional[str] = None) -> dict:
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            who = require_write_actor(db, project_id, actor, "integration_manage")
            deleted = event_intake.delete_connection(db, project_id=project_id,
                                                     connection_id=connection_id, actor=who)
        return {"deleted": deleted, "connection_id": connection_id}

    @router.post("/v1/intake/connections/{connection_id}/test")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def test_intake_connection(connection_id: str, body: Optional[ActorBody] = None) -> dict:
        """Do the secrets resolve, does the adapter's own check pass, what does the inbox say.
        Records the result on the connection's health."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_write_actor(db, project_id, (body or ActorBody()).actor, "integration_manage")
            try:
                return event_intake.test_connection(db, settings(), project_id=project_id,
                                                    connection_id=connection_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/intake/connections/{connection_id}/test-message")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def post_intake_test_message(connection_id: str, body: Optional[ActorBody] = None) -> dict:
        """Check the secrets, then post BOSS's own test message through the intake adapter.
        Does not replace /test: that one only checks secrets."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_write_actor(db, project_id, (body or ActorBody()).actor, "integration_manage")
            try:
                return event_intake.post_test_message(db, settings(), project_id=project_id,
                                                      connection_id=connection_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/intake/connections/{connection_id}/test-message")
    @permit(P.INTEGRATION_READ, scope=Scope.of("intake_connection", path="connection_id"))
    def get_intake_test_message(connection_id: str) -> dict:
        """Whether a person has answered the latest test post. Does not post again."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_reader(db, project_id)
            try:
                return event_intake.confirm_test_message(db, project_id=project_id,
                                                         connection_id=connection_id)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/intake/connections/{connection_id}/rotate-secret")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def rotate_intake_secret(connection_id: str, request: Request,
                             body: Optional[RotateBody] = None) -> dict:
        """A new BOSS-minted signing secret, shown ONCE; the old one stops verifying at once."""
        body = body or RotateBody()
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            actor = require_write_actor(db, project_id, body.actor, "integration_manage")
            try:
                out = event_intake.rotate_secret(db, project_id=project_id,
                                                 connection_id=connection_id, actor=actor,
                                                 field_name=body.field)
            except Exception as error:  # noqa: BLE001
                _fail(error)
        return _with_url(request, out)

    def _same_org(db, request: Request, project_id: str) -> None:
        """identity_provider:manage is org-only: the connection must be in the caller's org."""
        try:
            from .. import identity_guard

            ctx = identity_guard.request_auth(request)
        except Exception:  # noqa: BLE001 — a bare test app has no identity middleware
            ctx = None
        if ctx is None:
            return
        org = db.execute(sa_text(
            "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": project_id}).scalar()
        if str(org or "") != str(ctx.principal.org_id):
            raise HTTPException(status_code=404, detail={"error": "not_found",
                                                         "message": "no such connection."})

    @router.put("/v1/intake/connections/{connection_id}/sso-attestation")
    @permit(P.IDENTITY_PROVIDER_MANAGE, scope=Scope.org())
    def attest_intake_connection(connection_id: str, body: AttestationBody,
                                 request: Request) -> dict:
        """An identity-provider admin attests the source signs people in through this IdP, so
        chat users with a verified email in its domains are auto-linked (never guests)."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            _same_org(db, request, project_id)
            actor = require_write_actor(db, project_id, body.actor, "integration_manage")
            try:
                return event_intake.set_sso_attestation(
                    db, project_id=project_id, connection_id=connection_id, actor=actor,
                    identity_provider_id=body.identity_provider_id, domains=body.domains)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.delete("/v1/intake/connections/{connection_id}/sso-attestation")
    @permit(P.IDENTITY_PROVIDER_MANAGE, scope=Scope.org())
    def clear_intake_attestation(connection_id: str, request: Request,
                                 actor: Optional[str] = None) -> dict:
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            _same_org(db, request, project_id)
            who = require_write_actor(db, project_id, actor, "integration_manage")
            try:
                return event_intake.clear_sso_attestation(db, project_id=project_id,
                                                          connection_id=connection_id, actor=who)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── people links (E7) ────────────────────────────────────────────────────────────────────

    @router.get("/v1/intake/connections/{connection_id}/people")
    @permit(P.INTEGRATION_READ, scope=Scope.of("intake_connection", path="connection_id"))
    def intake_connection_people(connection_id: str, request: Request, limit: int = 200,
                                 offset: int = 0) -> dict:
        """Who each chat user of this connection is in BOSS: linked (method, when), revoked in
        the last 30 days, and the chat users heard from who are not linked (guests flagged);
        plus the SSO attestation. Never a link code. WHO the BOSS person is (name, email) is
        directory data: only a caller holding user:read sees it (people_visible)."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_reader(db, project_id)
            try:
                return event_intake.connection_people(
                    db, project_id=project_id, connection_id=connection_id,
                    reveal=people_visible(request, db), limit=limit, offset=offset)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/intake/connections/{connection_id}/people/prompts")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def prompt_intake_people(connection_id: str, body: Optional[PromptBody] = None) -> dict:
        """Send link prompts to not-linked people, or resend one. Never a guest, never a code."""
        body = body or PromptBody()
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            require_write_actor(db, project_id, body.actor, "integration_manage")
            try:
                return event_intake.prompt_people(
                    db, settings(), project_id=project_id, connection_id=connection_id,
                    external_user_id=body.external_user_id or None, resend=body.resend)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/intake/connections/{connection_id}/people/{link_id}/revoke")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def revoke_intake_person(connection_id: str, link_id: str,
                             body: Optional[ActorBody] = None) -> dict:
        """An admin ends one chat user's link; their next message gets a new link prompt."""
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            actor = require_write_actor(db, project_id, (body or ActorBody()).actor,
                                        "integration_manage")
            try:
                return event_intake.revoke_person_link(db, project_id=project_id,
                                                       connection_id=connection_id,
                                                       link_id=link_id, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    def _set_status(connection_id: str, body: Optional[ActorBody], status: str) -> dict:
        body = body or ActorBody()
        with begin(engine) as db:
            project_id = _connection_project(db, connection_id)
            actor = require_write_actor(db, project_id, body.actor, "integration_manage")
            try:
                return event_intake.set_connection_status(
                    db, project_id=project_id, connection_id=connection_id, actor=actor,
                    status=status, reason=body.reason)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/intake/connections/{connection_id}/pause")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def pause_intake_connection(connection_id: str, body: Optional[ActorBody] = None) -> dict:
        """Paused: deliveries are still answered 2xx (the source must not retry) and dropped."""
        return _set_status(connection_id, body, "paused")

    @router.post("/v1/intake/connections/{connection_id}/resume")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("intake_connection", path="connection_id"))
    def resume_intake_connection(connection_id: str, body: Optional[ActorBody] = None) -> dict:
        return _set_status(connection_id, body, "active")

    # ── rules (the console's rule list; source, agent, enabled) ─────────────────────────────

    @router.get("/v1/intake/rules")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def list_project_intake_rules(project_id: str, source: Optional[str] = None,
                                  agent: Optional[str] = None,
                                  enabled: Optional[bool] = None) -> dict:
        """Intake routes for the project. `source` is a connection id or a kind (`slack`).
        `agent` is the agent key. `enabled` is true for on, false for paused; omit it for both."""
        with begin(engine) as db:
            require_reader(db, project_id)
            return {"rules": agent_triggers.list_intake_rules(
                db, project_id, source=source or None, agent=agent or None, enabled=enabled)}

    # ── events (E5) and dead letters (E6) ────────────────────────────────────────────────────

    @router.get("/v1/intake/events")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_intake_events(project_id: str, request: Request, status: Optional[str] = None,
                           connection_id: Optional[str] = None, q: Optional[str] = None,
                           hours: Optional[int] = None, limit: int = 100,
                           before: Optional[str] = None) -> dict:
        """The stream: newest first, each with its status chip (routed / ignored / filtered /
        refused / failed / dead / shed / digest), and the chips' counts for the period. The BOSS
        person behind a chat user is withheld without user:read (people_visible)."""
        with begin(engine) as db:
            require_reader(db, project_id)
            return event_intake.list_events(db, project_id, status=status,
                                            connection_id=connection_id, q=q,
                                            since_hours=hours, limit=limit, before=before,
                                            reveal=people_visible(request, db))

    @router.get("/v1/intake/events/{intake_event_id}")
    @permit(P.INTEGRATION_READ, scope=Scope.of("intake_event", path="intake_event_id"))
    def get_intake_event(intake_event_id: str, request: Request) -> dict:
        """One event in full: envelope, guards (screen verdict, loop hops), the raw body, the
        fires it caused and their runs. Who the chat user is in BOSS (principal_id, emails) only
        with user:read (people_visible); otherwise withheld and `people_redacted: true`."""
        with begin(engine) as db:
            row = event_intake.get_event(db, intake_event_id,
                                         reveal=people_visible(request, db))
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            require_reader(db, str(row.get("project_id") or ""))
            return row

    @router.get("/v1/intake/dead-letters")
    @permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"))
    def list_intake_dead_letters(project_id: str, request: Request,
                                 connection_id: Optional[str] = None, limit: int = 100) -> dict:
        with begin(engine) as db:
            require_reader(db, project_id)
            return event_intake.dead_letters(db, project_id, limit=limit,
                                             connection_id=connection_id,
                                             reveal=people_visible(request, db))

    @router.post("/v1/intake/events/replay")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
    def replay_intake_events(body: ReplayBody) -> dict:
        """Reprocess: only routes that have not admitted a run for the event fire. Force (with a
        typed reason, logged with your name): every route runs once more."""
        with begin(engine) as db:
            actor = require_write_actor(db, body.project_id, body.actor, "integration_manage")
            try:
                return event_intake.replay(db, project_id=body.project_id,
                                           ids=body.intake_event_ids, actor=actor,
                                           mode=body.mode, reason=body.reason)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── the front door ───────────────────────────────────────────────────────────────────────

    @router.api_route("/v1/intake/{kind}/{connection}", methods=["POST", "OPTIONS"],
                      status_code=202)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK, verifier=event_intake.receive)
    async def receive_intake(kind: str, connection: str, request: Request):
        """A source's delivery. Public by necessity; nothing is stored unless the adapter's own
        verification (Standard Webhooks / BOSS v1 / X-Hub-Signature-256, on the raw bytes) accepts
        it under this connection's secret. Answers the source's success code once the row is
        committed; a duplicate, a paused connection and a storm are 2xx too; 401 a bad signature;
        404 an unknown URL; 413 too large; 503 only when the database is down."""
        raw, too_large = await _read_bounded(request)
        if too_large:
            return JSONResponse(status_code=413, content={"status": "too_large"})
        req = RawRequest(method=request.method,
                         headers={k.lower(): v for k, v in request.headers.items()}, body=raw,
                         query=dict(request.query_params), path=str(request.url.path))
        result = await run_in_threadpool(event_intake.receive, engine, kind, connection, req,
                                         settings=settings())
        headers = {str(k): str(v) for k, v in (result.get("headers") or {}).items()}
        if result.get("body") is None or int(result["http_status"]) == 204:
            return Response(status_code=int(result["http_status"]), headers=headers)
        if result.get("media_type") and result["media_type"] != "application/json" and isinstance(
                result.get("body"), (str, bytes)):
            return Response(content=result["body"], status_code=int(result["http_status"]),
                            media_type=str(result["media_type"]), headers=headers)
        return JSONResponse(status_code=int(result["http_status"]), content=result["body"],
                            headers=headers)

    return router
