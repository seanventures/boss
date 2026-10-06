"""Agents, delegation, tokens and connections: the identity API stream S3 owns (IDENTITY.md §10).

    GET  /.well-known/boss-jwks.json                 public keys only (active + retiring)
    POST /v1/identity/token                          RFC 8693 exchange; run renewal
    POST /v1/identity/introspect                     the egress service credential only (A40)
    POST /v1/identity/spawn                          the run supervisor: an actor assertion for a child run
    POST /v1/identity/credentials/github             JIT fallback, policy-gated (A59)
    /v1/identity/agents…                             Passports: read, ceiling, rules, suspend, resume, revoke
    /v1/identity/delegations…, /me/delegations…      grants: list, create (people only), revoke, cascade preview
    /v1/identity/connections…, /me/connections…      vaulted connections: list, disconnect

Identity comes only from the verified credential (session, key or run token) — never from a body
field. Error bodies name a code, never an exception's text or a secret.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public
from boss_engine.identity import cascade, delegation, grants, passports, signing_keys, tokens, vault
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.delegation import CascadeTrigger, GrantRequest, TokenExchangeRequest

TOKEN_FIELDS = ("grant_type", "subject_token", "subject_token_type", "actor_token", "actor_token_type",
                "audience", "scope", "requested_token_type")


def _engine():
    from boss_engine.db import get_engine

    return get_engine()


def _ctx(request: Request):
    """The request's AuthContext from its ONE verified credential (S1's authenticate)."""
    existing = getattr(request.state, "identity", None)
    if existing is not None:
        return existing
    from boss_engine.identity import authn

    from ..identity_guard import _credentials

    ctx = authn.authenticate(_credentials(request), db=None)
    if ctx is None:
        raise AuthnError("unauthenticated", 401)
    return ctx


def _err(code: str, status: int, **extra: Any) -> JSONResponse:
    headers = {}
    if extra.get("retry_after"):
        headers["Retry-After"] = str(extra.pop("retry_after"))
    if status == 401:
        headers["WWW-Authenticate"] = 'DPoP error="invalid_token"'
    body = {"error": code, **{k: v for k, v in extra.items() if v is not None}}
    return JSONResponse(status_code=status, content=body, headers=headers or None)


def _map(exc: Exception) -> JSONResponse:
    if isinstance(exc, tokens.TokenError):
        return _err(exc.code, exc.status, retry_after=exc.retry_after)
    if isinstance(exc, AuthnError):
        return _err(exc.code, exc.status)
    if isinstance(exc, grants.GrantRefused):
        return _err(exc.code, exc.status, **{k: v for k, v in exc.detail.items() if k in ("permission", "permissions", "agent", "project", "repo", "max_days")})
    if isinstance(exc, passports.PassportError):
        return _err(exc.code, exc.status)
    if isinstance(exc, vault.VaultError):
        return _err(exc.code if exc.status < 500 else "vault_error", exc.status)
    if isinstance(exc, PermissionError):
        return _err("forbidden", 403)
    raise exc


def _grant_json(g) -> dict[str, Any]:
    return {"id": g.id, "user": g.user_principal_id, "agents": list(g.agent_ids),
            "permissions": sorted(g.permissions), "projects": list(g.project_ids), "resources": dict(g.resources),
            "allow_subdelegation": g.allow_subdelegation, "allowed_children": list(g.allowed_children),
            "max_depth": g.max_depth, "status": g.status.value, "task_run_id": g.task_run_id,
            "purpose": g.purpose, "created_at": g.created_at.isoformat(),
            "expires_at": g.expires_at.isoformat() if g.expires_at else None}


def _passport_json(p) -> dict[str, Any]:
    return {"agent_id": p.agent_id, "role_key": p.role_key, "status": p.status, "sponsor": p.sponsor_principal_id,
            "ceiling": sorted(p.ceiling), "ceiling_preset": p.ceiling_preset, "external_ceiling": dict(p.external_ceiling),
            "can_spawn": p.can_spawn, "allowed_children": list(p.allowed_children), "max_depth": p.max_depth,
            "autonomous": p.autonomous, "version": p.version, "reissued_from": p.reissued_from}


def _report_json(r) -> dict[str, Any]:
    return {"trigger": r.trigger.value, "target": r.target_id, "dry_run": r.dry_run,
            "grants_revoked": list(r.grants_revoked), "grants_suspended": list(r.grants_suspended),
            "run_tokens_revoked": r.run_tokens_revoked, "egress_credentials_revoked": r.egress_credentials_revoked,
            "github_tokens_revoked": r.github_tokens_revoked, "runs_halted": list(r.runs_halted),
            "schedules_paused": list(r.schedules_paused), "passports_suspended": list(r.passports_suspended),
            "sessions_revoked": r.sessions_revoked}


async def _form(request: Request) -> dict[str, str]:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        data = await request.json()
    else:
        data = dict(await request.form())
    # Only the RFC 8693 fields are read. A client-supplied `act` or `boss_chain` is never a field
    # here: the chain is assembled from verified tokens alone (A44).
    return {k: str(data[k]) for k in TOKEN_FIELDS if isinstance(data, dict) and k in data and data[k] is not None}


def build_identity_agents_router() -> APIRouter:
    router = APIRouter(tags=["identity"])

    # ---- tokens ---------------------------------------------------------------------------------
    @router.get("/.well-known/boss-jwks.json")
    @public(reason=PublicReason.JWKS)
    def jwks_document():
        with _engine().begin() as db:
            doc = signing_keys.jwks_document(db=db)
        return JSONResponse(doc, headers={"Cache-Control": "public, max-age=300"})

    @router.post("/v1/identity/token")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=tokens.exchange_token)
    async def token(request: Request):
        form = await _form(request)
        htu = str(request.url).split("?", 1)[0]
        proof = request.headers.get("dpop")
        from starlette.concurrency import run_in_threadpool

        def run():
            with _engine().begin() as db:
                if form.get("grant_type") == tokens.GRANT_TYPE_RENEW:
                    return tokens.renew_run_token(form.get("subject_token", ""), dpop_proof=proof, htm="POST",
                                                  htu=htu, db=db)
                if form.get("grant_type") != delegation.GRANT_TYPE_TOKEN_EXCHANGE:
                    raise tokens.TokenError("unsupported_grant_type", 400, "grant_type")
                req = TokenExchangeRequest(
                    subject_token=form.get("subject_token", ""), subject_token_type=form.get("subject_token_type", ""),
                    actor_token=form.get("actor_token", ""), actor_token_type=form.get("actor_token_type", ""),
                    audience=form.get("audience", ""), scope=frozenset(form.get("scope", "").split()),
                    requested_token_type=form.get("requested_token_type", delegation.TOKEN_TYPE_RUN),
                    dpop_proof=proof)
                return tokens.exchange_token(req, htm="POST", htu=htu, db=db)

        try:
            issued = await run_in_threadpool(run)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return JSONResponse({"access_token": issued.access_token, "issued_token_type": issued.issued_token_type,
                             "token_type": issued.token_type, "expires_in": issued.expires_in,
                             "scope": " ".join(sorted(issued.scope))},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @router.post("/v1/identity/introspect")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=tokens.egress_service_key_ok)
    async def introspect(request: Request):
        # Never public in effect: only the egress proxy's service credential may ask (A40).
        if not tokens.egress_service_key_ok(request.headers.get("x-boss-egress-key")):
            return _err("unauthenticated", 401)
        form = dict(await request.form()) if not request.headers.get("content-type", "").startswith(
            "application/json") else await request.json()
        with _engine().begin() as db:
            return JSONResponse(tokens.introspect(str(form.get("token", "")), db=db),
                                headers={"Cache-Control": "no-store"})

    @router.post("/v1/identity/spawn")
    @authenticated()
    async def spawn(request: Request):
        """The supervisor's half of spawn_subagent: an actor assertion for a child run of the CALLER's
        run, bound to the child's DPoP key. The exchange (and every check) happens at /token."""
        body = await request.json()
        try:
            ctx = _ctx(request)
            if ctx.run_token is None:
                return _err("run_token_required", 403)
            child_agent, child_name, child_jkt = str(body["child_agent"]), str(body["child_name"]), str(body["child_jkt"])
            if not child_name.replace("-", "").replace("_", "").isalnum():
                return _err("bad_child_name", 422)
            with _engine().begin() as db:
                assertion = tokens.mint_actor_assertion(
                    parent=ctx.run_token, child_agent_id=child_agent,
                    child_run_id=f"{ctx.run_token.task.run_id}/{child_name}", child_jkt=child_jkt, db=db)
        except KeyError:
            return _err("invalid_request", 400)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return JSONResponse({"actor_token": assertion, "actor_token_type": delegation.TOKEN_TYPE_ACTOR},
                            headers={"Cache-Control": "no-store"})

    @router.post("/v1/identity/credentials/github")
    @authenticated()
    def jit_github(request: Request):
        try:
            ctx = _ctx(request)
            if ctx.run_token is None:
                return _err("run_token_required", 403)
            with _engine().begin() as db:
                cred = vault.get_credential(vault.ConnectionRef("github", None), vault.CredentialPurpose.GITHUB_JIT,
                                            ctx=ctx, task=ctx.run_token.task, db=db)
        except Exception as exc:  # noqa: BLE001
            from boss_engine.identity import github_app

            if isinstance(exc, github_app.GitHubRefused):
                return _err(exc.code, exc.status)
            return _map(exc)
        return JSONResponse({"token": cred.token, "expires_at": cred.expires_at.isoformat(),
                             "scopes": sorted(cred.scopes)}, headers={"Cache-Control": "no-store"})

    # ---- Passports ----------------------------------------------------------------------------------
    @router.get("/v1/identity/agents")
    @permit(P.AGENT_PASSPORT_READ, scope=Scope.org())
    def list_agents(request: Request):
        from sqlalchemy import text

        ctx = _ctx(request)
        with _engine().begin() as db:
            ids = db.execute(text("SELECT id FROM identity_principals WHERE org_id = :o AND kind = 'agent' "
                                  "ORDER BY display_name"), {"o": ctx.principal.org_id}).scalars().all()
            return {"agents": [_passport_json(p) for p in (passports.get(i, db=db) for i in ids) if p]}

    @router.get("/v1/identity/agents/{agent_id}")
    @permit(P.AGENT_PASSPORT_READ, scope=Scope.org())
    def get_agent(agent_id: str, request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            p = passports.get(agent_id, db=db)
        if p is None or p.org_id != ctx.principal.org_id:
            return _err("not_found", 404)
        return _passport_json(p)

    @router.put("/v1/identity/agents/{agent_id}/ceiling")
    @permit(P.AGENT_PASSPORT_MANAGE, scope=Scope.org())
    async def put_ceiling(agent_id: str, request: Request):
        body = await request.json()
        try:
            with _engine().begin() as db:
                p = passports.set_ceiling(_ctx(request), agent_id, frozenset(body.get("permissions") or []), db=db)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return _passport_json(p)

    @router.patch("/v1/identity/agents/{agent_id}")
    @permit(P.AGENT_PASSPORT_MANAGE, scope=Scope.org())
    async def patch_agent(agent_id: str, request: Request):
        body = await request.json()
        allowed = {"can_spawn", "allowed_children", "max_depth", "sponsor", "external_ceiling"}
        if set(body) - allowed:
            return _err("unknown_fields", 422)
        try:
            with _engine().begin() as db:
                p = passports.set_rules(_ctx(request), agent_id, db=db, can_spawn=body.get("can_spawn"),
                                        allowed_children=tuple(body["allowed_children"]) if "allowed_children" in body else None,
                                        max_depth=body.get("max_depth"), sponsor_principal_id=body.get("sponsor"),
                                        external_ceiling=body.get("external_ceiling"))
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return _passport_json(p)

    def _passport_action(action: str):
        async def handler(agent_id: str, request: Request):
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
            reason = str((body or {}).get("reason") or action)[:300]
            try:
                with _engine().begin() as db:
                    fn = {"suspend": passports.suspend, "resume": passports.resume, "revoke": passports.revoke}[action]
                    out = fn(_ctx(request), agent_id, reason=reason, db=db)
            except Exception as exc:  # noqa: BLE001
                return _map(exc)
            return _passport_json(out) if isinstance(out, passports.Passport) else _report_json(out)
        return handler

    router.post("/v1/identity/agents/{agent_id}/suspend")(
        permit(P.AGENT_PASSPORT_SUSPEND, scope=Scope.org())(_passport_action("suspend")))
    router.post("/v1/identity/agents/{agent_id}/resume")(
        permit(P.AGENT_PASSPORT_SUSPEND, scope=Scope.org())(_passport_action("resume")))
    router.post("/v1/identity/agents/{agent_id}/revoke")(
        permit(P.AGENT_PASSPORT_REVOKE, scope=Scope.org())(_passport_action("revoke")))

    @router.get("/v1/identity/agents/{agent_id}/cascade-preview")
    @permit(P.AGENT_PASSPORT_READ, scope=Scope.org())
    def agent_cascade_preview(agent_id: str, request: Request, action: str = "suspend"):
        trigger = CascadeTrigger.PASSPORT_REVOKED if action == "revoke" else CascadeTrigger.PASSPORT_SUSPENDED
        ctx = _ctx(request)
        with _engine().begin() as db:
            p = passports.get(agent_id, db=db)
            if p is None or p.org_id != ctx.principal.org_id:
                return _err("not_found", 404)
            return _report_json(cascade.preview(trigger, agent_id, actor=ctx, db=db))

    # ---- delegation grants ----------------------------------------------------------------------------
    @router.get("/v1/identity/me/delegations")
    @authenticated(self_only=True)
    def my_delegations(request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            return {"delegations": [_grant_json(g) for g in grants.for_person(ctx.principal.id, db=db)]}

    @router.post("/v1/identity/me/delegations")
    @permit(P.DELEGATION_GRANT, scope=Scope.org())
    async def create_delegation(request: Request):
        body = await request.json()
        known = {"agents", "permissions", "projects", "resources", "allow_subdelegation", "allowed_children",
                 "max_depth", "expires_at", "task_run_id", "purpose"}
        if not isinstance(body, dict) or set(body) - known:
            return _err("unknown_fields", 422)          # mass assignment: no user/org/status fields
        try:
            req = GrantRequest(
                agent_ids=tuple(body.get("agents") or ()), permissions=frozenset(body.get("permissions") or ()),
                project_ids=tuple(body.get("projects") or ()), resources=body.get("resources") or {},
                allow_subdelegation=bool(body.get("allow_subdelegation", False)),
                allowed_children=tuple(body.get("allowed_children") or ()),
                max_depth=int(body.get("max_depth", delegation.DEFAULT_MAX_DEPTH)),
                expires_at=datetime.fromisoformat(body["expires_at"]) if body.get("expires_at") else None,
                task_run_id=body.get("task_run_id"), purpose=str(body.get("purpose") or ""))
            with _engine().begin() as db:
                g = grants.create_grant(_ctx(request), req, db=db)
        except (TypeError, ValueError):
            return _err("invalid_request", 422)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return JSONResponse(_grant_json(g), status_code=201)

    def _revoke_grant(request: Request, grant_id: str, *, own_only: bool):
        ctx = _ctx(request)
        if ctx.run_token is not None:
            return _err("forbidden", 403, permission="delegation:revoke_any")
        with _engine().begin() as db:
            g = grants.load(grant_id, db=db)
            if g is None or g.org_id != ctx.principal.org_id or (own_only and g.user_principal_id != ctx.principal.id):
                return _err("not_found", 404)
            report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, grant_id, actor=ctx,
                                            reason="revoked by " + ("owner" if own_only else "admin"), db=db)
        return _report_json(report)

    @router.delete("/v1/identity/me/delegations/{grant_id}")
    @authenticated(self_only=True)
    def revoke_my_delegation(grant_id: str, request: Request):
        try:
            return _revoke_grant(request, grant_id, own_only=True)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.get("/v1/identity/delegations")
    @permit(P.DELEGATION_READ_ALL, scope=Scope.org())
    def all_delegations(request: Request, user: Optional[str] = None):
        from sqlalchemy import text

        ctx = _ctx(request)
        with _engine().begin() as db:
            ids = db.execute(text("SELECT id FROM identity_delegation_grants WHERE org_id = :o "
                                  "AND (:u IS NULL OR user_principal_id = :u) ORDER BY created_at DESC LIMIT 500"),
                             {"o": ctx.principal.org_id, "u": user}).scalars().all()
            return {"delegations": [_grant_json(grants.load(i, db=db)) for i in ids]}

    @router.delete("/v1/identity/delegations/{grant_id}")
    @permit(P.DELEGATION_REVOKE_ANY, scope=Scope.org())
    def revoke_any_delegation(grant_id: str, request: Request):
        try:
            return _revoke_grant(request, grant_id, own_only=False)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.get("/v1/identity/delegations/{grant_id}/cascade-preview")
    @authenticated()
    def grant_cascade_preview(grant_id: str, request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            g = grants.load(grant_id, db=db)
            if g is None or g.org_id != ctx.principal.org_id:
                return _err("not_found", 404)
            if g.user_principal_id != ctx.principal.id:
                from boss_engine.identity import authz
                from boss_engine.identity.scope import ResolvedScope

                if not authz.check(ctx, "delegation:read_all", ResolvedScope.org_level(g.org_id), db=db).allowed:
                    return _err("not_found", 404)
            return _report_json(cascade.preview(CascadeTrigger.GRANT_REVOKED, grant_id, actor=ctx, db=db))

    # ---- connections ------------------------------------------------------------------------------------
    def _connections(db, org_id: str, owner: Optional[str]):
        from sqlalchemy import text

        rows = db.execute(text(
            "SELECT id, owner_principal_id, kind, provider, provider_subject, connection_name, granted_scopes, status, "
            " created_at, last_used_at, access_expires_at FROM identity_vault_items WHERE org_id = :o "
            " AND kind IN ('oauth_user','legacy_pat') AND (:u IS NULL OR owner_principal_id = :u) "
            " ORDER BY created_at DESC"), {"o": org_id, "u": owner}).mappings().all()
        return [{"id": r["id"], "owner": r["owner_principal_id"], "kind": r["kind"], "provider": r["provider"],
                 "account": r["provider_subject"], "name": r["connection_name"], "scopes": r["granted_scopes"] or [],
                 "status": r["status"], "legacy": r["kind"] == "legacy_pat",
                 "created_at": r["created_at"].isoformat(),
                 "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None} for r in rows]

    @router.get("/v1/identity/me/connections")
    @authenticated(self_only=True)
    def my_connections(request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            return {"connections": _connections(db, ctx.principal.org_id, ctx.principal.id)}

    @router.delete("/v1/identity/me/connections/{item_id}")
    @authenticated(self_only=True)
    def disconnect_mine(item_id: str, request: Request):
        try:
            ctx = _ctx(request)
            with _engine().begin() as db:
                mine = [c["id"] for c in _connections(db, ctx.principal.org_id, ctx.principal.id)]
                if item_id not in mine:
                    return _err("not_found", 404)
                vault.revoke(item_id, ctx=ctx, reason="disconnected by owner", db=db)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return {"revoked": item_id}

    @router.get("/v1/identity/connections")
    @permit(P.CONNECTION_READ_ALL, scope=Scope.org())
    def all_connections(request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            return {"connections": _connections(db, ctx.principal.org_id, None)}

    @router.delete("/v1/identity/connections/{item_id}")
    @permit(P.CONNECTION_REVOKE_ANY, scope=Scope.org())
    def disconnect_any(item_id: str, request: Request):
        try:
            ctx = _ctx(request)
            with _engine().begin() as db:
                if item_id not in [c["id"] for c in _connections(db, ctx.principal.org_id, None)]:
                    return _err("not_found", 404)
                vault.revoke(item_id, ctx=ctx, reason="disconnected by admin", db=db)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return {"revoked": item_id}

    return router
