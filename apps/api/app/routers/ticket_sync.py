"""Ticket systems after the start (BOSSAPP-1047 Phase B2, wireframes T6 + T1's sign-in):

* WRITE-BACK (T6): GET /v1/work-items/{id}/ticket-sync is the timeline of what BOSS wrote to the
  ticket (each effect a receipt); POST .../ticket-sync/retry sends the failed ones again.
* INBOUND: POST /v1/ticket-systems/{id}/webhook (integration:manage) creates or rotates the
  connection's webhook secret and shows the URL + secret once.
  POST /v1/webhooks/tickets/{connection_id} is public — the connector's own
  verify_webhook, under the connection's secret, is what authenticates it — and a ticket that
  gained the pick-up label is offered (a card) or imported and started (services/ticket_sync).
* A PERSON'S OWN ACCOUNT ("Connect with Atlassian"): /v1/ticket-systems/{id}/oauth/start sends
  the browser to the provider; /v1/ticket-systems/oauth/callback (public, state-bound) keeps the
  person's tokens in the vault; DELETE .../oauth/me disconnects; GET /v1/me/ticket-identities
  lists the person's linked accounts with `email_matches`.

The connection, browse, import and start routes are routers/ticket_systems.py (B1). Mounted
from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

import html
import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public

from .. import identity_verifiers
from ..identity_guard import writing_get


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Optional[str] = Field(default=None, max_length=200)


class WebhookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Optional[str] = Field(default=None, max_length=200)


def _principal(request: Request) -> Optional[str]:
    """The verified principal of this request, or None (a bare provider redirect)."""
    from ..identity_guard import request_auth

    try:
        ctx = getattr(request.state, "identity", None) or request_auth(request)
    except Exception:  # noqa: BLE001 — a refused credential is no principal here
        return None
    return str(ctx.principal.id) if ctx is not None else None


def _person(request: Request) -> str:
    """The signed-in PERSON: a sign-in to a ticket system is always somebody's own."""
    from boss_engine.identity.principal import PrincipalKind

    from ..identity_guard import request_auth

    ctx = getattr(request.state, "identity", None) or request_auth(request)
    if ctx is None or ctx.principal.kind != PrincipalKind.USER or ctx.run_token is not None:
        raise HTTPException(status_code=403, detail="Sign in to BOSS as yourself to connect "
                                                    "your own ticket-system account.")
    return str(ctx.principal.id)


_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _callback_url(request: Request) -> str:
    """Where the provider sends the browser back: the install's PUBLIC origin (BOSS_PUBLIC_URL or
    the origin bound at the network claim), else — for a console on this machine — the loopback
    address the request came in on. Never an arbitrary Host header."""
    from boss_engine.connectors.tickets.auth import CALLBACK_PATH

    return _public_base(request, "so Atlassian can send you back to BOSS") + CALLBACK_PATH


def _public_base(request: Request, why: str) -> str:
    """The install's public origin (see _callback_url), without a trailing slash."""
    from boss_engine.db import get_engine
    from boss_engine.identity.authn_support import origin_of, public_origin

    base = origin_of(os.environ.get("BOSS_PUBLIC_URL") or "")
    if not base:
        with get_engine().begin() as db:
            base = public_origin(db)
    if not base and (request.url.hostname or "").lower() in _LOOPBACK:
        base = f"{request.url.scheme}://{request.url.netloc}"
    if not base:
        raise HTTPException(status_code=409, detail={
            "error": "no_public_origin",
            "message": f"Set BOSS_PUBLIC_URL (the address people open BOSS at) {why}."})
    return base.rstrip("/")


def _page(title: str, body: str, ok: bool) -> Response:
    colour = "#34d399" if ok else "#f2647a"
    return Response(
        content=("<!doctype html><meta charset='utf-8'><title>BOSS — ticket sign-in</title>"
                 "<style>body{background:#090d17;color:#e6edf3;font:16px/1.6 system-ui,"
                 "sans-serif;display:grid;place-items:center;height:100vh;margin:0}"
                 f"div{{max-width:32rem;padding:2rem}}h1{{color:{colour};font-size:1.25rem;"
                 "margin:0 0 .75rem}p{margin:.5rem 0;opacity:.85}</style>"
                 f"<div><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>"
                 "<p>You can close this tab and return to BOSS.</p></div>"),
        media_type="text/html", status_code=200 if ok else 400)


def build_ticket_sync_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.connectors.tickets import TicketConnection
    from boss_engine.connectors.tickets import auth as ticket_auth
    from boss_engine.services import ticket_sync

    router = APIRouter(tags=["ticket-systems"])

    def _item_project(db, work_item_id: str) -> str:
        project_id = db.execute(
            text("SELECT project_id FROM work_items WHERE id = :id"),
            {"id": work_item_id}).scalar()
        if not project_id:
            raise HTTPException(status_code=404, detail="Work item not found.")
        return str(project_id)

    def _connection(db, connection_id: str) -> TicketConnection:
        row = ticket_sync.provider_row(db, connection_id)
        if row is None or row["status"] != "active":
            raise HTTPException(status_code=404, detail="Ticket connection not found.")
        conn = ticket_sync.connection_from_row(db, row, app_settings=settings())
        return conn

    def _oauth_failure(error: Exception):
        if isinstance(error, ticket_auth.OAuthError):
            status = 409 if error.code == "not_configured" else 400
            raise HTTPException(status_code=status,
                                detail={"error": error.code, "message": str(error)}) from error
        raise error

    # ------------------------------------------------------------------ T6: write-back
    @router.get("/v1/work-items/{work_item_id}/ticket-sync")
    @permit(P.WORK_ITEM_READ, scope=Scope.of("work_item", path="work_item_id"))
    def ticket_sync_timeline(work_item_id: str) -> dict:
        """What BOSS wrote to this work item's ticket, oldest first: comments, moves and links,
        each with its status (a failed one says why and whether a retry can help)."""
        with begin(engine) as db:
            project_id = _item_project(db, work_item_id)
            require_reader(db, project_id)
            return ticket_sync.timeline(db, project_id, work_item_id)

    @router.post("/v1/work-items/{work_item_id}/ticket-sync/retry")
    @permit(P.WORK_TRACKER_WRITE, scope=Scope.of("work_item", path="work_item_id"))
    def ticket_sync_retry(work_item_id: str, payload: Optional[RetryBody] = None) -> dict:
        """Send this ticket's failed write-backs again, as the person retrying (their own sign-in
        when they have one). An effect whose outcome is unknown is never resent."""
        with begin(engine) as db:
            project_id = _item_project(db, work_item_id)
            actor = require_write_actor(db, project_id, (payload or RetryBody()).actor,
                                        "task_create")
        result = ticket_sync.retry_failed(engine, settings(), project_id, work_item_id,
                                          actor=actor)
        with begin(engine) as db:
            result["timeline"] = ticket_sync.timeline(db, project_id, work_item_id)
        return result

    # ------------------------------------------------------------------ inbound
    @router.post("/v1/webhooks/tickets/{connection_id}", status_code=202)
    @public(reason=PublicReason.HMAC_SIGNED_WEBHOOK,
            verifier=identity_verifiers.ticket_webhook_signature)
    async def receive_ticket_webhook(connection_id: str, request: Request):
        """A ticket system's delivery. Public by necessity; nothing happens unless the
        connector's verify_webhook accepts it under this connection's secret (a Jira webhook
        registered without a secret carries ?token=, passed on as X-Boss-Webhook-Token)."""
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        token = request.query_params.get("token")
        if token and "x-boss-webhook-token" not in headers:
            headers["x-boss-webhook-token"] = token
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(ticket_sync.ingest_webhook, engine, settings(),
                                         connection_id, headers, raw)
        status = result.get("status")
        if status == "rejected":
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
        if status == "not_found":
            raise HTTPException(status_code=404, detail="Unknown ticket connection.")
        if status == "invalid":
            raise HTTPException(status_code=400, detail="Invalid webhook payload.")
        if status in ("ignored", "already_in_boss", "already_asked", "unreadable"):
            return JSONResponse(status_code=200, content=result)
        return JSONResponse(status_code=202, content=result)

    @router.post("/v1/ticket-systems/{connection_id}/webhook")
    @permit(P.INTEGRATION_MANAGE, scope=Scope.of("work_tracker_provider", path="connection_id"))
    def create_ticket_webhook(connection_id: str, request: Request,
                              payload: Optional[WebhookBody] = None) -> dict:
        """Create or rotate this connection's inbound webhook: a new secret, minted into the
        secret store (the connection keeps only its reference), and the URL to paste into the
        ticket system. Both are shown ONCE; rotating stops the old secret at once."""
        base = _public_base(request, "so the ticket system can reach BOSS")
        with begin(engine) as db:
            row = ticket_sync.provider_row(db, connection_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Ticket connection not found.")
            actor = require_write_actor(db, str(row["project_id"]),
                                        (payload or WebhookBody()).actor, "integration_manage")
            try:
                return ticket_sync.create_webhook(db, settings(), connection_id, actor=actor,
                                                  public_base=base)
            except ticket_sync.TicketSyncError as error:
                raise HTTPException(status_code=error.status, detail={
                    "error": error.code, "message": str(error)}) from error

    # ------------------------------------------------------------------ a person's own account
    def _start(connection_id: str, request: Request) -> dict[str, Any]:
        person = _person(request)
        redirect_uri = _callback_url(request)
        with begin(engine) as db:
            conn = _connection(db, connection_id)
            try:
                return ticket_auth.begin_oauth(conn, person, redirect_uri=redirect_uri)
            except Exception as error:  # noqa: BLE001
                _oauth_failure(error)
                raise

    @writing_get(router, "/v1/ticket-systems/{connection_id}/oauth/start")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.of("work_tracker_provider", path="connection_id"),
            writes_on_get=True)
    def ticket_oauth_start(connection_id: str, request: Request):
        """"Connect with Atlassian": the browser goes to the provider's consent page (it records a
        single-use pending sign-in, so it is a writing GET; the console's POST twin at
        .../oauth/start/load answers {authorize_url} for a tab it opens itself, as does a GET
        that asks for JSON)."""
        started = _start(connection_id, request)
        if request.method == "POST" or "application/json" in (request.headers.get("accept")
                                                              or ""):
            return JSONResponse({"authorize_url": started["authorize_url"]})
        return RedirectResponse(started["authorize_url"], status_code=302)

    @router.get("/v1/ticket-systems/oauth/callback")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK,
            verifier=identity_verifiers.ticket_oauth_state)
    def ticket_oauth_callback(request: Request, state: str = "", code: str = "",
                              error: str = "", error_description: str = "") -> Response:
        """Where Atlassian sends the browser back. Public by necessity (a redirect carries no
        BOSS credential); safe because nothing happens without a state BOSS signed, bound to the
        project, the connection and the person, single-use and ten minutes long. Answers a page,
        never a token."""
        if error:
            return _page("Not connected", f"The provider said: {error_description or error}.",
                         False)
        try:
            payload = ticket_auth.read_state(state)
        except ticket_auth.OAuthError as refused:
            return _page("Not connected", str(refused), False)
        provider = str(payload.get("c") or "").rsplit(":", 1)[-1]
        with begin(engine) as db:
            row_id = db.execute(text(
                "SELECT id FROM work_tracker_providers WHERE project_id = :p AND provider_key = :k"),
                {"p": str(payload.get("p") or ""), "k": provider}).scalar()
            if row_id is None:
                return _page("Not connected", "That ticket connection no longer exists.", False)
            conn = _connection(db, str(row_id))
            try:
                done = ticket_auth.complete_oauth(conn, state, code,
                                                  completed_by=_principal(request))
            except ticket_auth.OAuthError as refused:
                return _page("Not connected", str(refused), False)
        identity: dict[str, Any] = {}
        with begin(engine) as db:
            conn = _connection(db, str(row_id))
            try:
                identity = ticket_sync.provider_identity(conn, done["person"])
            except Exception:  # noqa: BLE001 — the sign-in stands without the email
                identity = {}
            ticket_sync.remember_identity(db, str(conn.project_id), conn.connection_id,
                                          done["person"], identity)
            mine = [i for i in ticket_sync.my_identities(db, done["person"],
                                                         str(conn.project_id))
                    if i["provider"] == conn.provider]
        shown = identity.get("email") or identity.get("name") or ""
        if mine and not mine[0]["email_matches"]:
            return _page("Connected — please confirm",
                         f"You signed in as {shown or 'an account'}, which is not your BOSS "
                         "email. Confirm in BOSS that this account is yours before BOSS acts "
                         "as you.", True)
        return _page("Connected", f"BOSS will act as {shown or 'you'} in "
                                  f"{conn.provider.replace('_', ' ').title()}.", True)

    @router.delete("/v1/ticket-systems/{connection_id}/oauth/me")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.of("work_tracker_provider", path="connection_id"))
    def ticket_oauth_disconnect(connection_id: str, request: Request) -> dict:
        """Disconnect your own account: the vault item is shredded; BOSS falls back to the
        connection's bot where the connection allows it."""
        person = _person(request)
        with begin(engine) as db:
            conn = _connection(db, connection_id)
            removed = ticket_auth.disconnect_person(conn, person, actor=person)
        return {"disconnected": removed, "connection_id": connection_id}

    @router.get("/v1/me/ticket-identities")
    @authenticated(self_only=True, people_only=True)
    def my_ticket_identities(request: Request, project_id: Optional[str] = None) -> dict:
        """Your linked ticket-system accounts. `email_matches` is false when the provider's
        email is not your verified BOSS email: the console asks you to confirm it is yours."""
        person = _person(request)
        with begin(engine) as db:
            return {"identities": ticket_sync.my_identities(db, person, project_id)}

    return router
