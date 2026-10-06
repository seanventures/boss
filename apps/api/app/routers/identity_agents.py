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

PROVIDER_LABELS = {"github": "GitHub"}


def _callback_url(request: Request, provider: str) -> str:
    import os

    base = (os.environ.get("BOSS_PUBLIC_URL") or str(request.base_url)).rstrip("/")
    return f"{base}/v1/identity/me/connections/{provider}/callback"


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
        return _err(exc.code, exc.status, **{k: v for k, v in exc.detail.items() if k in ("permission", "key", "provider")})
    if isinstance(exc, vault.VaultError):
        return _err(exc.code if exc.status < 500 else "vault_error", exc.status)
    if isinstance(exc, PermissionError):
        return _err("forbidden", 403)
    raise exc


def _hue(principal_id: str) -> int:
    import hashlib

    return int(hashlib.sha256(principal_id.encode()).hexdigest()[:4], 16) % 12


def _names(db, ids) -> dict[str, str]:
    from sqlalchemy import text

    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = db.execute(text("SELECT id, display_name FROM identity_principals WHERE id = ANY(:i)"), {"i": list(ids)}).all()
    return {r[0]: r[1] for r in rows}


_STATUS = {"active": "active", "suspended": "paused", "revoked": "revoked", "expired": "expired"}


def _grant_json(g, db) -> dict[str, Any]:
    """The console's `Delegation` (apps/web/lib/identity/types.ts)."""
    from sqlalchemy import text

    names = _names(db, [g.user_principal_id, *g.agent_ids])
    gh = grants.github_resources(g)
    last = db.execute(text("SELECT last_used_at FROM identity_delegation_grants WHERE id = :g"), {"g": g.id}).scalar()
    last_run = db.execute(text("SELECT run_id FROM identity_run_credentials WHERE grant_id = :g ORDER BY issued_at DESC "
                               "LIMIT 1"), {"g": g.id}).scalar()
    person = names.get(g.user_principal_id, g.user_principal_id)
    return {
        "id": g.id, "kind": "task" if g.task_run_id else "standing",
        "user": {"id": g.user_principal_id, "name": person.split(" ")[0], "hue": _hue(g.user_principal_id)},
        "agents": [{"id": a, "name": names.get(a, a)} for a in g.agent_ids],
        "label": g.purpose or "acting for me", "permissions": sorted(g.permissions), "projects": list(g.project_ids),
        "github": ({"repos": list(gh.get("repos") or []), "contents": gh.get("contents"),
                    "pull_requests": gh.get("pull_requests"), "ref_rule": "boss/*" if gh.get("contents") == "write" else None,
                    "as_user": bool(gh.get("as_user"))} if gh else None),
        "subdelegation": {"allowed": g.allow_subdelegation, "to": list(g.allowed_children)},
        "max_depth": g.max_depth, "created_at": g.created_at.isoformat(),
        "expires_at": g.expires_at.isoformat() if g.expires_at else None, "purpose": g.purpose or None,
        "status": _STATUS.get(g.status.value, g.status.value),
        "last_used_at": last.isoformat() if last else None, "last_used_run": last_run,
        "task_run_id": g.task_run_id,
    }


def _passport_json(p, db=None) -> dict[str, Any]:
    """The console's `Passport` (apps/web/lib/identity/types.ts)."""
    out = {"id": p.agent_id, "agent_id": p.agent_id, "name": p.role_key.capitalize(), "role_key": p.role_key,
           "title": (p.ceiling_preset or "agent").replace("_", " ").capitalize(), "sponsor": None,
           "ceiling_preset": p.ceiling_preset, "ceiling": sorted(p.ceiling), "external_ceiling": dict(p.external_ceiling),
           "can_spawn": p.can_spawn, "allowed_children": list(p.allowed_children), "max_depth": p.max_depth,
           "autonomous": p.autonomous, "status": p.status, "sealed": p.version > 1, "active_grants": 0,
           "last_run_at": None, "lineage": [], "version": p.version, "reissued_from": p.reissued_from}
    if db is not None:
        from sqlalchemy import text

        names = _names(db, [p.agent_id, p.sponsor_principal_id])
        out["name"] = names.get(p.agent_id, out["name"])
        out["sponsor"] = {"id": p.sponsor_principal_id, "name": names.get(p.sponsor_principal_id, p.sponsor_principal_id),
                          "hue": _hue(p.sponsor_principal_id)}
        out["active_grants"] = int(db.execute(text(
            "SELECT count(*) FROM identity_delegation_grants WHERE status = 'active' AND :a = ANY(agent_ids)"),
            {"a": p.agent_id}).scalar_one())
        last = db.execute(text(
            "SELECT max(issued_at) FROM identity_run_credentials r WHERE r.subject_principal_id = :a OR EXISTS "
            "(SELECT 1 FROM jsonb_array_elements_text(r.chain) h WHERE split_part(h, '#', 1) = :a)"),
            {"a": p.agent_id}).scalar()
        out["last_run_at"] = last.isoformat() if last else None
    return out


def _report_json(r) -> dict[str, Any]:
    """The console's `CascadePreview` {items, halts}, plus the raw `report`."""
    runs = list(r.runs_halted)
    children = [g for g in r.grants_revoked if g != r.target_id]
    items = [{"kind": "runs", "label": "Halts running agents",
              "detail": ", ".join(runs[:3]) + (f" and {len(runs) - 3} more" if len(runs) > 3 else "") or "none running",
              "count": len(runs)}]
    if r.run_tokens_revoked:
        items.append({"kind": "tokens", "label": "Revokes run tokens", "detail": f"{r.run_tokens_revoked} live tokens",
                      "count": r.run_tokens_revoked})
    if r.github_tokens_revoked and r.egress_credentials_revoked:
        items.append({"kind": "github", "label": "Revokes the GitHub token",
                      "detail": "installation tokens for the runs' repos", "count": r.egress_credentials_revoked})
    if children:
        items.append({"kind": "grants", "label": "Revokes the child grant" if len(children) == 1 else "Revokes child grants",
                      "detail": ", ".join(children[:3]), "count": len(children)})
    if r.grants_suspended:
        items.append({"kind": "grants", "label": "Pauses grants", "detail": ", ".join(r.grants_suspended[:3]),
                      "count": len(r.grants_suspended)})
    if r.schedules_paused:
        items.append({"kind": "schedules", "label": "Pauses a schedule" if len(r.schedules_paused) == 1 else "Pauses schedules",
                      "detail": ", ".join(r.schedules_paused[:3]), "count": len(r.schedules_paused)})
    return {"halts": len(runs), "items": items, "report": {
        "trigger": r.trigger.value, "target": r.target_id, "dry_run": r.dry_run,
        "grants_revoked": list(r.grants_revoked), "grants_suspended": list(r.grants_suspended),
        "run_tokens_revoked": r.run_tokens_revoked, "egress_credentials_revoked": r.egress_credentials_revoked,
        "github_tokens_revoked": r.github_tokens_revoked, "runs_halted": runs,
        "schedules_paused": list(r.schedules_paused), "passports_suspended": list(r.passports_suspended),
        "sessions_revoked": r.sessions_revoked}}


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
            return {"agents": [_passport_json(p, db) for p in (passports.get(i, db=db) for i in ids) if p]}

    def _get_passport(agent_id: str, request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            p = passports.get(agent_id, db=db)
            if p is None or p.org_id != ctx.principal.org_id:
                return _err("not_found", 404)
            return _passport_json(p, db)

    @router.get("/v1/identity/agents/{agent_id}")
    @permit(P.AGENT_PASSPORT_READ, scope=Scope.org())
    def get_agent(agent_id: str, request: Request):
        return _get_passport(agent_id, request)

    @router.get("/v1/identity/agents/{agent_id}/passport")
    @permit(P.AGENT_PASSPORT_READ, scope=Scope.org())
    def get_agent_passport(agent_id: str, request: Request):
        return _get_passport(agent_id, request)

    async def _patch_passport(agent_id: str, request: Request):
        """One PATCH for everything a Passport holds: `ceiling`, `external_ceiling`, the spawn rules,
        the depth cap and the sponsor. Unknown fields are 422 (no mass assignment)."""
        body = await request.json()
        allowed = {"ceiling", "can_spawn", "allowed_children", "max_depth", "sponsor", "external_ceiling"}
        if not isinstance(body, dict) or set(body) - allowed:
            return _err("unknown_fields", 422)
        try:
            ctx = _ctx(request)
            with _engine().begin() as db:
                current = passports.get(agent_id, db=db)
                if current is None or current.org_id != ctx.principal.org_id:
                    return _err("not_found", 404)
                if "ceiling" in body:
                    passports.set_ceiling(ctx, agent_id, frozenset(body.get("ceiling") or []), db=db)
                rules = {k: body[k] for k in body if k != "ceiling"}
                if rules:
                    passports.set_rules(ctx, agent_id, db=db, can_spawn=rules.get("can_spawn"),
                                        allowed_children=tuple(rules["allowed_children"]) if "allowed_children" in rules else None,
                                        max_depth=rules.get("max_depth"), sponsor_principal_id=rules.get("sponsor"),
                                        external_ceiling=rules.get("external_ceiling"))
                return _passport_json(passports.require(agent_id, db=db), db)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.patch("/v1/identity/agents/{agent_id}")
    @permit(P.AGENT_PASSPORT_MANAGE, scope=Scope.org())
    async def patch_agent(agent_id: str, request: Request):
        return await _patch_passport(agent_id, request)

    @router.patch("/v1/identity/agents/{agent_id}/passport")
    @permit(P.AGENT_PASSPORT_MANAGE, scope=Scope.org())
    async def patch_agent_passport(agent_id: str, request: Request):
        return await _patch_passport(agent_id, request)

    def _passport_action(action: str):
        async def handler(agent_id: str, request: Request):
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
            reason = str((body or {}).get("reason") or action)[:300]
            try:
                with _engine().begin() as db:
                    fn = {"suspend": passports.suspend, "resume": passports.resume, "revoke": passports.revoke}[action]
                    out = fn(_ctx(request), agent_id, reason=reason, db=db)
                    return _passport_json(passports.require(agent_id, db=db), db) if action == "resume" else \
                        {**_passport_json(passports.require(agent_id, db=db), db), "cascade": _report_json(out)}
            except Exception as exc:  # noqa: BLE001
                return _map(exc)
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
            return {"delegations": [_grant_json(g, db) for g in grants.for_person(ctx.principal.id, db=db)]}

    @router.post("/v1/identity/me/delegations")
    @permit(P.DELEGATION_GRANT, scope=Scope.org())
    async def create_delegation(request: Request):
        """Accepts the console's shape ({agents, permissions, projects, kind: task|standing, days,
        purpose}, plus `task_run_id` from a 409 consent_required) and the long form (resources,
        sub-delegation, max_depth, expires_at). `kind: task` without a run id is bound to a fresh
        `pending:` id that the person's next matching run claims."""
        body = await request.json()
        known = {"agents", "permissions", "projects", "resources", "allow_subdelegation", "allowed_children",
                 "max_depth", "expires_at", "task_run_id", "purpose", "kind", "days", "github"}
        if not isinstance(body, dict) or set(body) - known:
            return _err("unknown_fields", 422)          # mass assignment: no user/org/status fields
        try:
            import secrets as _secrets
            from datetime import timedelta, timezone

            kind = body.get("kind") or ("task" if body.get("task_run_id") else "standing")
            if kind not in ("task", "standing", "schedule"):
                return _err("invalid_request", 422)
            task_run_id = body.get("task_run_id")
            if kind == "task" and not task_run_id:
                task_run_id = grants.PENDING_PREFIX + _secrets.token_urlsafe(12)
            if task_run_id is not None and not (str(task_run_id).startswith(grants.PENDING_PREFIX)
                                                or str(task_run_id).startswith("run_")):
                return _err("invalid_request", 422)
            expires_at = datetime.fromisoformat(body["expires_at"]) if body.get("expires_at") else None
            if expires_at is None and body.get("days") and kind != "task":
                expires_at = datetime.now(timezone.utc) + timedelta(days=int(body["days"]))
            resources = dict(body.get("resources") or {})
            if body.get("github") and "github" not in resources:
                resources["github"] = body["github"]
            req = GrantRequest(
                agent_ids=tuple(body.get("agents") or ()), permissions=frozenset(body.get("permissions") or ()),
                project_ids=tuple(body.get("projects") or ()), resources=resources,
                allow_subdelegation=bool(body.get("allow_subdelegation", False)),
                allowed_children=tuple(body.get("allowed_children") or ()),
                max_depth=int(body.get("max_depth", delegation.DEFAULT_MAX_DEPTH)),
                expires_at=expires_at, task_run_id=task_run_id, purpose=str(body.get("purpose") or ""))
            with _engine().begin() as db:
                g = grants.create_grant(_ctx(request), req, db=db)
                return JSONResponse(_grant_json(g, db), status_code=201)
        except (TypeError, ValueError):
            return _err("invalid_request", 422)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    async def _dry_run(request: Request) -> bool:
        if not request.headers.get("content-type", "").startswith("application/json"):
            return request.query_params.get("dry_run") in ("1", "true")
        try:
            body = await request.json()
        except ValueError:
            return False
        return bool(isinstance(body, dict) and body.get("dry_run"))

    def _revoke_grant(request: Request, grant_id: str, *, own_only: bool, dry_run: bool = False):
        ctx = _ctx(request)
        if ctx.run_token is not None:
            return _err("forbidden", 403, permission="delegation:revoke_any")
        with _engine().begin() as db:
            g = grants.load(grant_id, db=db)
            if g is None or g.org_id != ctx.principal.org_id or (own_only and g.user_principal_id != ctx.principal.id):
                return _err("not_found", 404)
            if g.status.value in ("revoked", "expired"):
                return _err("revoked_is_terminal", 409)
            report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, grant_id, actor=ctx, dry_run=dry_run,
                                            reason="revoked by " + ("owner" if own_only else "admin"), db=db)
            return _report_json(report)

    @router.delete("/v1/identity/me/delegations/{grant_id}")
    @authenticated(self_only=True)
    def revoke_my_delegation(grant_id: str, request: Request):
        try:
            return _revoke_grant(request, grant_id, own_only=True)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.post("/v1/identity/me/delegations/{grant_id}/revoke")
    @authenticated(self_only=True)
    async def revoke_my_delegation_post(grant_id: str, request: Request):
        """{dry_run: true} answers the cascade preview and changes nothing."""
        dry = await _dry_run(request)
        try:
            return _revoke_grant(request, grant_id, own_only=True, dry_run=dry)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.get("/v1/identity/delegations")
    @permit(P.DELEGATION_READ_ALL, scope=Scope.org())
    def all_delegations(request: Request, user: Optional[str] = None):
        from sqlalchemy import text

        ctx = _ctx(request)
        with _engine().begin() as db:
            ids = db.execute(text("SELECT id FROM identity_delegation_grants WHERE org_id = :o "
                                  "AND (CAST(:u AS TEXT) IS NULL OR user_principal_id = :u) "
                                  "ORDER BY created_at DESC LIMIT 500"),
                             {"o": ctx.principal.org_id, "u": user}).scalars().all()
            return {"delegations": [_grant_json(grants.load(i, db=db), db) for i in ids]}

    @router.delete("/v1/identity/delegations/{grant_id}")
    @permit(P.DELEGATION_REVOKE_ANY, scope=Scope.org())
    def revoke_any_delegation(grant_id: str, request: Request):
        try:
            return _revoke_grant(request, grant_id, own_only=False)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.post("/v1/identity/delegations/{grant_id}/revoke")
    @permit(P.DELEGATION_REVOKE_ANY, scope=Scope.org())
    async def revoke_any_delegation_post(grant_id: str, request: Request):
        """{dry_run: true} answers the cascade preview (console's CascadePreview) and changes nothing."""
        dry = await _dry_run(request)
        try:
            return _revoke_grant(request, grant_id, own_only=False, dry_run=dry)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.post("/v1/identity/delegations/{grant_id}/extend")
    @permit(P.DELEGATION_GRANT, scope=Scope.org())
    async def extend_delegation(grant_id: str, request: Request):
        """{days}: only the person who granted it; re-checked against their live access."""
        body = await request.json()
        try:
            days = int((body or {}).get("days") or 30)
            ctx = _ctx(request)
            with _engine().begin() as db:
                g = grants.load(grant_id, db=db)
                if g is None or g.org_id != ctx.principal.org_id:
                    return _err("not_found", 404)
                return _grant_json(grants.extend_grant(ctx, grant_id, days, db=db), db)
        except (TypeError, ValueError):
            return _err("invalid_request", 422)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    @router.post("/v1/identity/delegations/{grant_id}/narrow")
    @authenticated()
    async def narrow_delegation(grant_id: str, request: Request):
        """{permissions[]}: the new, smaller set (the grantor, or `delegation:revoke_any`)."""
        body = await request.json()
        perms = (body or {}).get("permissions")
        if not isinstance(perms, list):
            return _err("invalid_request", 422)
        try:
            ctx = _ctx(request)
            with _engine().begin() as db:
                g = grants.load(grant_id, db=db)
                if g is None or g.org_id != ctx.principal.org_id:
                    return _err("not_found", 404)
                return _grant_json(grants.narrow_grant(ctx, grant_id, frozenset(map(str, perms)), db=db), db)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)

    def _may_read_grant(ctx, g, db) -> bool:
        if g is None or g.org_id != ctx.principal.org_id:
            return False
        if g.user_principal_id == ctx.principal.id:
            return True
        from boss_engine.identity import authz
        from boss_engine.identity.scope import ResolvedScope

        return authz.check(ctx, "delegation:read_all", ResolvedScope.org_level(g.org_id), db=db).allowed

    @router.get("/v1/identity/delegations/{grant_id}/cascade-preview")
    @authenticated()
    def grant_cascade_preview(grant_id: str, request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            if not _may_read_grant(ctx, grants.load(grant_id, db=db), db):
                return _err("not_found", 404)
            return _report_json(cascade.preview(CascadeTrigger.GRANT_REVOKED, grant_id, actor=ctx, db=db))

    @router.get("/v1/identity/delegations/{grant_id}/claims")
    @authenticated()
    def grant_token_claims(grant_id: str, request: Request, run: Optional[str] = None):
        """"Show token claims": the claims of the newest live run token under this grant (or for
        `run`), WITHOUT signature, raw token, DPoP key or any secret. `scope` is the live
        intersection the token is decided against now (the ledger keeps only a scope hash)."""
        from boss_engine.identity import claims_view

        ctx = _ctx(request)
        with _engine().begin() as db:
            g = grants.load(grant_id, db=db)
            if not _may_read_grant(ctx, g, db):
                return _err("not_found", 404)
            return claims_view.for_grant(g, run_id=run, db=db)

    # ---- connections ------------------------------------------------------------------------------------
    def _connections(db, org_id: str, owner: Optional[str]):
        from sqlalchemy import text

        rows = db.execute(text(
            "SELECT id, owner_principal_id, kind, provider, provider_subject, connection_name, granted_scopes, status, "
            " created_at, last_used_at, access_expires_at FROM identity_vault_items WHERE org_id = :o "
            " AND kind IN ('oauth_user','legacy_pat') AND (CAST(:u AS TEXT) IS NULL OR owner_principal_id = :u) "
            " ORDER BY created_at DESC"), {"o": org_id, "u": owner}).mappings().all()
        return [{"id": r["id"], "owner": r["owner_principal_id"], "kind": r["kind"],
                 "provider": PROVIDER_LABELS.get(r["provider"] or "", r["provider"]), "provider_key": r["provider"],
                 "account": r["provider_subject"], "name": r["connection_name"], "scopes": r["granted_scopes"] or [],
                 "status": r["status"], "legacy": r["kind"] == "legacy_pat",
                 "linked_at": r["created_at"].isoformat(), "created_at": r["created_at"].isoformat(),
                 "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None} for r in rows]

    @router.get("/v1/identity/me/connections")
    @authenticated(self_only=True)
    def my_connections(request: Request):
        ctx = _ctx(request)
        with _engine().begin() as db:
            return {"connections": _connections(db, ctx.principal.org_id, ctx.principal.id)}

    @router.post("/v1/identity/me/connections")
    @permit(P.CONNECTION_USE_OWN, scope=Scope.org())
    async def connect_mine(request: Request):
        """{provider} -> {redirect}: the provider's consent page. The callback below completes it
        for the SAME signed-in person (signed, expiring, single-use state)."""
        body = await request.json()
        provider = str((body or {}).get("provider") or "github").lower()
        if provider not in PROVIDER_LABELS:
            return _err("unknown_provider", 404)
        try:
            ctx = _ctx(request)
            if not ctx.is_human_session:
                return _err("people_only", 403)
            from boss_engine.identity import github_app

            url = github_app.authorize_url(ctx.principal.id, _callback_url(request, provider))
        except Exception as exc:  # noqa: BLE001
            from boss_engine.identity import github_app

            if isinstance(exc, github_app.GitHubRefused):
                return _err(exc.code, exc.status)
            return _map(exc)
        return {"redirect": url}

    @router.get("/v1/identity/me/connections/{provider}/callback")
    @authenticated(self_only=True)
    def connect_callback(provider: str, request: Request, code: str = "", state: str = ""):
        from fastapi.responses import RedirectResponse

        from boss_engine.identity import github_app

        if provider not in PROVIDER_LABELS:
            return _err("unknown_provider", 404)
        try:
            ctx = _ctx(request)
            if not ctx.is_human_session:
                return _err("people_only", 403)
            with _engine().begin() as db:
                github_app.complete_oauth(person_id=ctx.principal.id, org_id=ctx.principal.org_id, code=code,
                                          state=state, redirect_uri=_callback_url(request, provider), db=db)
        except github_app.GitHubRefused as exc:
            # Never a token or code in the redirect: only the outcome.
            return RedirectResponse(f"/account/agents?connect_error={exc.code}", status_code=303)
        except Exception as exc:  # noqa: BLE001
            return _map(exc)
        return RedirectResponse(f"/account/agents?connected={provider}", status_code=303)

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


def install_consent_handler(app) -> None:
    """Render `ConsentRequired` as a FLAT 409 body ({"error": "consent_required", ...}) wherever it
    is raised (build start, run start). Without it FastAPI still answers 409, as {"detail": {...}}."""
    from boss_engine.identity.consent import ConsentRequired

    async def handler(_request, exc: ConsentRequired):
        return JSONResponse(status_code=409, content=exc.body)

    app.add_exception_handler(ConsentRequired, handler)
