"""MCP sign-in, credentials and agent tool grants (BOSSAPP-846, 847, 848).

* Sign in (846): POST /v1/mcp/servers/{id}/oauth/start answers the provider's consent URL for
  the server's SHARED sign-in or an AGENT's own (people who manage the server), or — with
  `subject: "me"` — the signed-in PERSON's own ("Use my own account"). The provider sends the
  browser back to GET /v1/mcp/oauth/callback, which is public: the single-use `state` BOSS minted
  is what authenticates it (identity_verifiers.mcp_oauth_state), and it is the only thing that
  says which server and whose sign-in it was.
* Credentials (847): list who has signed in, disconnect one, set the server's (or a Kit
  connection's) credential policy, or keep a typed token as a person's or agent's own.
* Grants on request (848): list the tools agents were granted from a Grant card, revoke one.

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, permit, public

from .. import identity_verifiers


class ProjectBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class SignInBody(ProjectBody):
    #: "org" (the shared sign-in), "me" (the signed-in person's own) or "agent".
    subject: str = "org"
    role_key: Optional[str] = None


class PolicyBody(ProjectBody):
    policy: str


class TokenBody(ProjectBody):
    subject: str = "me"
    role_key: Optional[str] = None
    token: str


def _public_origin() -> str:
    """Where the provider sends the browser back: the install's PUBLIC origin, never the Host
    this request was addressed to (behind the console that is the API's internal address)."""
    from boss_engine.identity.authn_support import origin_of, public_origin

    base = origin_of(os.environ.get("BOSS_PUBLIC_URL") or "")
    if not base:
        from boss_engine.db import get_engine

        with get_engine().begin() as db:
            base = public_origin(db)
    if not base:
        raise HTTPException(status_code=409, detail="Set BOSS_PUBLIC_URL (the address people "
                                                    "open BOSS at) so a provider can send you back.")
    return base


def _principal(request: Request) -> Optional[str]:
    """The verified principal of this request, or None (no BOSS credential, e.g. a bare redirect)."""
    from ..identity_guard import request_auth

    try:
        ctx = getattr(request.state, "identity", None) or request_auth(request)
    except Exception:  # noqa: BLE001 — a refused credential is no principal here
        return None
    return str(ctx.principal.id) if ctx is not None else None


def _person(request: Request) -> str:
    """The signed-in PERSON (a usr_ principal) — "my own account" is never anyone else's."""
    from boss_engine.identity.principal import PrincipalKind

    from ..identity_guard import request_auth

    ctx = getattr(request.state, "identity", None) or request_auth(request)
    if ctx is None or ctx.principal.kind != PrincipalKind.USER or ctx.run_token is not None:
        raise HTTPException(status_code=403, detail="Sign in to BOSS as yourself to use your "
                                                    "own account.")
    return str(ctx.principal.id)


def build_mcp_credentials_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import connection_credentials as creds
    from boss_engine.services import mcp_oauth, tool_access_requests
    from boss_engine.services.mcp_registry import get_mcp_server

    router = APIRouter(tags=["mcp"])

    def opener():
        return begin(engine)

    def _server(connection, project_id: str, server_id: str) -> dict[str, Any]:
        server = get_mcp_server(connection, project_id, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found.")
        return server

    def _subject(connection, request: Request, project_id: str, subject: str,
                 role_key: Optional[str]) -> tuple[str, str]:
        if subject == "me":
            return "user", _person(request)
        if subject == "agent":
            role = str(role_key or "").strip().lower()
            if not role or not tool_access_requests._role_exists(connection, project_id, role):
                raise HTTPException(status_code=422, detail="Choose one of this project's agents.")
            return "agent", role
        if subject == "org":
            return "org", creds.org_of_project(connection, project_id)
        raise HTTPException(status_code=422, detail="subject must be org, me or agent.")

    def _fail(error: Exception):
        if isinstance(error, mcp_oauth.McpOAuthError):
            raise HTTPException(status_code=error.status,
                                detail={"error": error.code, "message": error.message}) from error
        if isinstance(error, (creds.CredentialError, tool_access_requests.ToolAccessError)):
            raise HTTPException(status_code=getattr(error, "status", 422),
                                detail=str(error)) from error
        raise error

    # ------------------------------------------------------------------ 846: sign in
    def _start(server_id: str, body: SignInBody, request: Request) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage"
                                        if body.subject != "me" else "agent_chat")
            _server(connection, body.project_id, server_id)
            kind, subject_id = _subject(connection, request, body.project_id, body.subject,
                                        body.role_key)
        redirect_uri = mcp_oauth.callback_url(_public_origin())
        try:
            return mcp_oauth.begin_sign_in(opener, settings(), body.project_id, server_id,
                                           subject_kind=kind, subject_id=subject_id,
                                           redirect_uri=redirect_uri, actor=actor,
                                           started_by=_principal(request))
        except Exception as error:  # noqa: BLE001
            _fail(error)
            raise

    @router.post("/v1/mcp/servers/{server_id}/oauth/start")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def start_sign_in(server_id: str, body: SignInBody, request: Request) -> dict[str, Any]:
        """The SHARED sign-in or an agent's own: {redirect} to open in a browser tab."""
        if body.subject == "me":
            raise HTTPException(status_code=422, detail="Use /oauth/mine for your own account.")
        return _start(server_id, body, request)

    @router.post("/v1/mcp/servers/{server_id}/oauth/mine")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.project(body="project_id"))
    def start_my_sign_in(server_id: str, body: ProjectBody, request: Request) -> dict[str, Any]:
        """"Use my own account": the signed-in person's own sign-in to this server."""
        return _start(server_id, SignInBody(project_id=body.project_id, actor=body.actor,
                                            subject="me"), request)

    @router.get("/v1/mcp/oauth/callback")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK,
            verifier=identity_verifiers.mcp_oauth_state)
    def finish_sign_in(request: Request, state: str = "", code: str = "",
                       error: str = "") -> Response:
        """Where the provider sends the browser back. Public by necessity (a redirect carries no
        BOSS credential); safe because nothing happens without a state BOSS minted, stored only
        as a hash, single-use and ten minutes long. Answers a page, never a token."""
        import html

        def page(title: str, body: str, ok: bool) -> Response:
            colour = "#34d399" if ok else "#f2647a"
            return Response(
                content=("<!doctype html><meta charset='utf-8'><title>BOSS — sign in</title>"
                         "<style>body{background:#090d17;color:#e6edf3;font:16px/1.6 system-ui,"
                         "sans-serif;display:grid;place-items:center;height:100vh;margin:0}"
                         f"div{{max-width:32rem;padding:2rem}}h1{{color:{colour};font-size:1.25rem;"
                         "margin:0 0 .75rem}p{margin:.5rem 0;opacity:.85}</style>"
                         f"<div><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>"
                         "<p>You can close this tab and return to BOSS.</p></div>"),
                media_type="text/html", status_code=200 if ok else 400)

        try:
            done = mcp_oauth.complete(opener, settings(), state=state, code=code, error=error,
                                      completed_by=_principal(request))
        except mcp_oauth.McpOAuthError as refused:
            return page("Not signed in", refused.message, False)
        except creds.CredentialError as refused:
            return page("Not signed in", str(refused), False)
        who = done["credential"].get("connected_as")
        return page("Signed in", f"BOSS is signed in to {done['server']}"
                                 + (f" as {who}." if who else "."), True)

    # ------------------------------------------------------------------ 847: credentials
    @router.get("/v1/mcp/servers/{server_id}/credentials")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def list_credentials(server_id: str, project_id: str, request: Request) -> dict[str, Any]:
        """Who has signed in (never a token), and what a call by the viewer would carry."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            server = _server(connection, project_id, server_id)
            rows = creds.list_credentials(connection, project_id, server_id=server_id)
            try:
                me: Optional[str] = _person(request)
            except HTTPException:
                me = None
            for row in rows:
                row["mine"] = bool(me) and row["subject_kind"] == "user" and row["subject_id"] == me
            uses = creds.explain(connection, project_id, server,
                                 creds.CredentialPrincipal(person_id=me)) if me else None
        return {"server_id": server_id, "credential_policy": server["credential_policy"],
                "auth_status": server["auth_status"], "credentials": rows, "you": uses}

    @router.post("/v1/mcp/servers/{server_id}/credential-policy")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def set_policy(server_id: str, body: PolicyBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                policy = creds.set_policy(connection, body.project_id, server_id, body.policy,
                                          actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
            return {"server_id": server_id, "credential_policy": policy}

    def _keep_token(server_id: str, body: TokenBody, request: Request, *, subject: str,
                    permission: str) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, permission)
            _server(connection, body.project_id, server_id)
            kind, subject_id = _subject(connection, request, body.project_id, subject,
                                        body.role_key)
            try:
                return creds.store(connection, body.project_id, server_id=server_id,
                                   subject_kind=kind, subject_id=subject_id, secret=body.token,
                                   kind="token", actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/mcp/servers/{server_id}/credentials/mine")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.project(body="project_id"))
    def keep_my_token(server_id: str, body: TokenBody, request: Request) -> dict[str, Any]:
        """A typed token as the signed-in PERSON's own, for a server that signs in with a token
        rather than OAuth. (The shared token is set on the server itself, Save & test.)"""
        return _keep_token(server_id, body, request, subject="me", permission="agent_chat")

    @router.post("/v1/mcp/servers/{server_id}/credentials/agent")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(body="project_id"))
    def keep_agent_token(server_id: str, body: TokenBody, request: Request) -> dict[str, Any]:
        """A typed token as one AGENT's own (per_agent servers), set by who manages the server."""
        return _keep_token(server_id, body, request, subject="agent", permission="pack_manage")

    def _disconnect(server_id: str, credential_id: str, project_id: str, request: Request,
                    actor: Optional[str], *, own_only: bool) -> dict[str, Any]:
        with begin(engine) as connection:
            rows = {r["id"]: r for r in creds.list_credentials(connection, project_id,
                                                                server_id=server_id)}
            row = rows.get(credential_id)
            if row is None:
                raise HTTPException(status_code=404, detail="No such credential.")
            if own_only and not (row["subject_kind"] == "user"
                                 and row["subject_id"] == _person(request)):
                raise HTTPException(status_code=403, detail="That is not your own sign-in.")
            who = require_write_actor(connection, project_id, actor,
                                      "agent_chat" if own_only else "pack_manage")
            creds.remove(connection, project_id, credential_id, actor=who)
        return {"removed": credential_id}

    @router.delete("/v1/mcp/servers/{server_id}/credentials/mine/{credential_id}")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.project(query="project_id"))
    def disconnect_mine(server_id: str, credential_id: str, project_id: str, request: Request,
                        actor: Optional[str] = None) -> dict[str, Any]:
        """Disconnect your OWN sign-in to this server."""
        return _disconnect(server_id, credential_id, project_id, request, actor, own_only=True)

    @router.delete("/v1/mcp/servers/{server_id}/credentials/{credential_id}")
    @permit(P.MCP_SERVER_REVIEW, scope=Scope.project(query="project_id"))
    def disconnect(server_id: str, credential_id: str, project_id: str, request: Request,
                   actor: Optional[str] = None) -> dict[str, Any]:
        """Disconnect any sign-in to this server (the shared one, an agent's, a person's)."""
        return _disconnect(server_id, credential_id, project_id, request, actor, own_only=False)

    @router.post("/v1/kit/{ability}/credential-policy")
    @permit(P.SKILL_MANAGE, scope=Scope.project(body="project_id"))
    def set_kit_policy(ability: str, body: PolicyBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                policy = creds.set_kit_policy(connection, body.project_id, ability, body.policy,
                                              actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
        return {"ability": ability, "credential_policy": policy}

    # ------------------------------------------------------------------ 848: grants on request
    @router.get("/v1/mcp/access-grants")
    @permit(P.MCP_SERVER_READ, scope=Scope.project(query="project_id"))
    def list_access_grants(project_id: str, server_id: Optional[str] = None,
                           role_key: Optional[str] = None, include_ended: bool = False
                           ) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"grants": tool_access_requests.list_grants(
                connection, project_id, server_id=server_id, role_key=role_key,
                include_ended=include_ended)}

    @router.post("/v1/mcp/access-grants/{grant_id}/revoke")
    @permit(P.AGENT_ABILITY_ASSIGN, scope=Scope.project(body="project_id"))
    def revoke_access_grant(grant_id: str, body: ProjectBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                grant = tool_access_requests.revoke_grant(connection, body.project_id, grant_id,
                                                          actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
        if grant is None:
            raise HTTPException(status_code=404, detail="No such grant.")
        return grant

    return router
