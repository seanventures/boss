"""SSO console sign-in and identity-provider administration (IDENTITY.md §4.8, §4.9, §4.15, §10).
Stream S1.

  GET  /v1/auth/sso/{provider_id}/start        302 to the IdP (state/nonce/PKCE bound to this browser)
  GET  /v1/auth/sso/{provider_id}/callback     OIDC / GitHub return (code + state)
  POST /v1/auth/sso/{provider_id}/callback     Apple form_post return
  GET  /saml/v2/metadata                       SP metadata
  POST /saml/v2/acs                            SAML POST binding
  /v1/identity/providers[...]                  provider admin, connection test, SCIM keys

Every callback ends in a 303 to the console with the session cookie set; the redirect URL never
carries a token, code or state (A92) — only `?error=<code>`, `?step=mfa` or `?linked=1`.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from boss_engine.db import begin
from boss_engine.identity import P, PublicReason, permit, public
from boss_engine.identity import api_keys, ids, social, sso_oidc, sso_saml, users, vault
from boss_engine.identity.audit import Outcome
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.authn_support import default_org_id, emit, origin_of
from boss_engine.identity.signin import Answer
from boss_engine.identity.sso_oidc import SsoError

from .identity_auth import current_context, error, facts, refusal


def _redirect(origin: str, answer: Answer) -> Response:
    """Translate a sign-in Answer into a console redirect. Only fixed, safe query values."""
    nxt = answer.body.get("next") if answer.status == 200 else None
    if nxt == "done":
        target = f"{origin}/"
    elif nxt == "mfa":
        target = f"{origin}/signin?step=mfa"
    elif nxt == "linked":
        target = f"{origin}/account?linked=1"
    elif nxt == "enrol":   # signed in, but an MFA-required role has no factor yet
        target = f"{origin}/enrol"
    else:
        code = str(answer.body.get("error") or "sso_failed")
        safe = code if code.replace("_", "").isalnum() and len(code) <= 40 else "sso_failed"
        target = f"{origin}/signin?error={quote(safe)}"
    response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    for cookie in answer.cookies:
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


def _fail(origin: str, code: str) -> Response:
    return _redirect(origin, Answer(401, {"error": code}))


def verify_sso_start(request: Request) -> None:
    """Start mints a fresh state for this browser; it authenticates nothing by itself (a link
    start requires a signed-in, stepped-up session, checked in the handler)."""


def verify_state_bound_callback(request: Request) -> None:
    """The OIDC callback's proof: `state` names a live challenge bound to this browser's pre-auth
    cookie, single use (sso_oidc.callback checks it, then spends it)."""


def verify_saml_assertion(request: Request) -> None:
    """The ACS's proof: a signed assertion answering a request this browser started
    (sso_saml.consume_response)."""


class _Rollback(Exception):
    """Leave the transaction (rolling it back) and answer with `response`."""

    def __init__(self, response: Response):
        super().__init__("rollback")
        self.response = response


# ---- provider admin bodies (allowlists: an unknown field is a 422) --------------------------------

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderBody(_Body):
    # THE ID IS KNOWN BEFORE SAVE. The redirect URL names the provider (/v1/auth/sso/{id}/callback),
    # and Google, Okta and friends ask for it BEFORE they hand out the client id and secret this
    # form needs — so the operator pasted a literal "<id>" into Google. The console reserves the id
    # when the Add sheet opens and sends it here; it has the server's own shape (idp_ + 26 base32)
    # and a taken one is 409. State and CSRF on the callback are unchanged: the id is not a secret.
    id: Optional[str] = Field(default=None, pattern="^idp_[a-z2-7]{26}$")
    protocol: str = Field(pattern="^(oidc|saml|oauth2)$")
    preset: str = Field(default="generic", pattern="^(google|github|microsoft|apple|okta|entra|keycloak|generic)$")
    name: str = Field(max_length=120)
    issuer: Optional[str] = Field(default=None, max_length=500)
    tenant: Optional[str] = Field(default=None, max_length=100, pattern="^[A-Za-z0-9.-]+$")
    client_id: Optional[str] = Field(default=None, max_length=500)
    client_secret: Optional[str] = Field(default=None, max_length=4000)
    scopes: list[str] = Field(default_factory=list, max_length=20)
    allowed_algs: list[str] = Field(default_factory=lambda: ["RS256", "ES256", "EdDSA"], max_length=10)
    saml_sso_url: Optional[str] = Field(default=None, max_length=1000)
    trusted_authn_contexts: Optional[list[str]] = Field(default=None, max_length=20)
    saml_sp_entity_id: Optional[str] = Field(default=None, max_length=500)
    saml_certificates: list[str] = Field(default_factory=list, max_length=2)
    domains: list[str] = Field(default_factory=list, max_length=50)
    jit_mode: str = Field(default="off", pattern="^(off|domain)$")
    trust_idp_mfa: bool = False
    enforce_sso: bool = False
    groups_claim: Optional[str] = Field(default=None, max_length=100)
    group_mappings: dict[str, str] = Field(default_factory=dict)
    client_auth: str = Field(default="client_secret", pattern="^(client_secret|private_key_jwt)$")
    client_private_key: Optional[str] = Field(default=None, max_length=8000)   # write-only PEM
    client_key_kid: Optional[str] = Field(default=None, max_length=200)
    client_assertion_issuer: Optional[str] = Field(default=None, max_length=200)


class ProviderPatch(_Body):
    name: Optional[str] = Field(default=None, max_length=120)
    client_id: Optional[str] = Field(default=None, max_length=500)
    client_secret: Optional[str] = Field(default=None, max_length=4000)
    scopes: Optional[list[str]] = None
    allowed_algs: Optional[list[str]] = None
    saml_sso_url: Optional[str] = Field(default=None, max_length=1000)
    trusted_authn_contexts: Optional[list[str]] = Field(default=None, max_length=20)
    saml_certificates: Optional[list[str]] = Field(default=None, max_length=2)
    domains: Optional[list[str]] = None
    jit_mode: Optional[str] = Field(default=None, pattern="^(off|domain)$")
    trust_idp_mfa: Optional[bool] = None
    enforce_sso: Optional[bool] = None
    groups_claim: Optional[str] = None
    group_mappings: Optional[dict[str, str]] = None
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")
    client_auth: Optional[str] = Field(default=None, pattern="^(client_secret|private_key_jwt)$")
    client_private_key: Optional[str] = Field(default=None, max_length=8000)
    client_key_kid: Optional[str] = Field(default=None, max_length=200)
    client_assertion_issuer: Optional[str] = Field(default=None, max_length=200)


class ScimKeyBody(_Body):
    name: str = Field(default="SCIM", max_length=120)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


def _authn_contexts(values: Optional[list[str]]) -> Optional[list[str]]:
    """The trusted AuthnContextClassRef set to store (None: BOSS's default list)."""
    cleaned = sorted({v.strip()[:300] for v in (values or []) if v and v.strip()})
    return cleaned or None


def _public_provider(p: sso_oidc.Provider) -> dict[str, Any]:
    """Everything an admin may see. No secret, no secret reference."""
    return {"id": p.id, "protocol": p.protocol, "preset": p.preset, "name": p.name, "issuer": p.issuer,
            "client_id": p.client_id if p.protocol != "saml" else None,
            "saml_sso_url": p.sso_url,
            "trusted_authn_contexts": list(p.trusted_authn_contexts or ()) if p.protocol == "saml" else None,
            "has_client_secret": bool(p.client_secret_vault_item_id), "scopes": list(p.scopes),
            "allowed_algs": list(p.allowed_algs), "saml_sp_entity_id": p.saml_sp_entity_id,
            "saml_certificate_count": len(p.saml_certificates), "domains": list(p.domains),
            "jit_mode": p.jit_mode, "trust_idp_mfa": p.trust_idp_mfa, "enforce_sso": p.enforce_sso,
            "groups_claim": p.groups_claim, "status": p.status,
            "client_auth": p.client_auth, "has_client_key": bool(p.client_key_vault_item_id),
            "client_key_kid": p.client_key_kid, "client_assertion_issuer": p.client_assertion_issuer}


def _validate_algs(algs: list[str]) -> list[str]:
    if not algs or any(a not in sso_oidc.SAFE_ALGS for a in algs):
        raise ValueError("allowed_algs must be asymmetric algorithms (never none or HS*)")
    return sorted(set(algs))


def _resolve_issuer(body: ProviderBody) -> Optional[str]:
    preset = sso_oidc.PRESETS.get(body.preset)
    if body.protocol == "oauth2":
        if body.preset != "github":
            raise ValueError("oauth2 is the GitHub preset only")
        return sso_oidc.GITHUB_ISSUER
    if body.protocol == "oidc" and preset and preset.get("protocol") == "oidc":
        if "issuer_template" in preset:
            if not body.tenant or body.tenant in ("common", "organizations", "consumers"):
                raise ValueError("the Microsoft preset needs your tenant id (an exact issuer)")
            return preset["issuer_template"].format(tenant=body.tenant)
        return preset["issuer"]
    if not body.issuer:
        raise ValueError("issuer is required")
    if body.protocol == "oidc" and not origin_of(body.issuer):
        raise ValueError("issuer must be an absolute URL")
    return body.issuer


def _store_secret(db: Any, org_id: str, provider_id: str, secret: str) -> str:
    return vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                       provider=provider_id, secret=secret, db=db)


def _client_key(db: Any, org_id: str, provider_id: str, pem: Optional[str],
                kid: Optional[str]) -> tuple[str, str]:
    """§4.8 private_key_jwt: vault the uploaded private key (validated) or a newly generated P-256
    one; answer (vault item id, kid). The key never leaves the vault; GET …/jwks shows its public half."""
    pem = (pem or "").strip() or sso_oidc.generate_client_key()
    sso_oidc.client_key_alg(pem)                       # ValueError for a key BOSS will not sign with
    return _store_secret(db, org_id, provider_id, pem), (kid or sso_oidc.client_key_thumbprint(pem))


def _write_mappings(db: Any, provider_id: str, mappings: dict[str, str], org_id: str) -> None:
    db.execute(text("DELETE FROM identity_provider_group_mappings WHERE provider_id = :p"), {"p": provider_id})
    for external, gid in mappings.items():
        ok = db.execute(text("SELECT 1 FROM identity_groups WHERE id = :g AND org_id = :o"),
                        {"g": gid, "o": org_id}).first()
        if ok is None:
            raise ValueError("group_mappings names an unknown group")
        db.execute(text("INSERT INTO identity_provider_group_mappings (provider_id, external_group, group_id) "
                        "VALUES (:p, :e, :g)"), {"p": provider_id, "e": external[:200], "g": gid})


def scim_principal_id(provider_id: str) -> str:
    from boss_engine.identity.scim import provisioning_principal

    return provisioning_principal(provider_id)


def _own_provider(db: Any, ctx: Any, provider_id: str) -> Optional[sso_oidc.Provider]:
    provider = sso_oidc.load_provider(db, provider_id, active_only=False)
    return provider if provider and provider.org_id == ctx.principal.org_id else None


def build_identity_sso_router() -> APIRouter:
    router = APIRouter(tags=["identity-sso"])

    # ---- sign-in ----------------------------------------------------------------------------------

    @router.get("/v1/auth/sso/{provider_id}/start")
    @public(reason=PublicReason.AUTH_ENDPOINT, verifier=verify_sso_start)
    def sso_start(provider_id: str, request: Request, purpose: str = "login"):
        with begin() as db:
            origin = facts(request, db).origin or ""
            if not origin:
                # H2: a redirect URI / ACS URL is only ever the BOUND public origin, never Host.
                return error(409, "public_origin_unset")
            provider = sso_oidc.load_provider(db, provider_id)
            if provider is None:
                return error(404, "not_found")
            link_principal: Optional[str] = None
            if purpose == "link":
                from .identity_me import is_fresh

                try:
                    ctx = current_context(request, db)
                except AuthnError as exc:
                    return refusal(exc)
                if ctx.session is None or not is_fresh(ctx) or provider.protocol == "saml":
                    return error(401, "step_up_required")
                link_principal = ctx.principal.id
            elif purpose != "login":
                return error(422, "invalid_purpose")
            try:
                if provider.protocol == "saml":
                    flow = sso_saml.start(db, provider, origin=origin)
                else:
                    flow = sso_oidc.start(db, provider, origin=origin, purpose=purpose,
                                          link_principal=link_principal)
            except SsoError as exc:
                return _fail(origin, exc.code)
            response = RedirectResponse(flow.url, status_code=302, headers={"Cache-Control": "no-store"})
            response.raw_headers.append((b"set-cookie", flow.cookie.encode("latin-1")))
            return response

    def _oidc_callback(provider_id: str, request: Request, params: dict[str, str]) -> Response:
        with begin() as db:
            f = facts(request, db)
            origin = f.origin or ""
            provider = sso_oidc.load_provider(db, provider_id)
            if provider is None or provider.protocol == "saml":
                return _fail(origin, "sso_failed")
            try:
                result = sso_oidc.callback(db, provider, params=params, preauth=f.preauth, origin=origin)
            except SsoError as exc:
                emit(db, org_id=provider.org_id, event_type="auth.login.failed", outcome=Outcome.DENIED,
                     ip=f.ip, user_agent=f.user_agent, request_id=f.request_id,
                     details={"reason": exc.code, "provider_id": provider.id})
                return _fail(origin, exc.code)
            answer = social.resolve(db, provider, result.identity, f, purpose=result.purpose,
                                    link_principal=result.link_principal)
            return _redirect(origin, answer)

    @router.get("/v1/auth/sso/{provider_id}/callback")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK, verifier=verify_state_bound_callback)
    def sso_callback(provider_id: str, request: Request):
        params = {k: v for k, v in request.query_params.items() if k in ("code", "state", "iss", "error")}
        return _oidc_callback(provider_id, request, params)

    @router.post("/v1/auth/sso/{provider_id}/callback")
    @public(reason=PublicReason.OAUTH_STATE_BOUND_CALLBACK, verifier=verify_state_bound_callback)
    async def sso_callback_post(provider_id: str, request: Request):
        form = await request.form()
        params = {k: str(v) for k, v in form.items() if k in ("code", "state", "iss", "error")}
        return await run_in_threadpool(_oidc_callback, provider_id, request, params)

    @router.get("/saml/v2/metadata")
    @public(reason=PublicReason.SAML_SIGNED_ASSERTION, verifier=verify_saml_assertion)
    def saml_metadata(request: Request, provider_id: Optional[str] = None):
        with begin() as db:
            origin = facts(request, db).origin or ""
            if not origin:
                return error(409, "public_origin_unset")
            provider = sso_oidc.load_provider(db, provider_id) if provider_id else None
            body = sso_saml.metadata(origin, sso_saml.sp_entity_id(provider, origin))
        return Response(body, media_type="application/samlmetadata+xml")

    def _acs(request: Request, saml_response: str) -> Response:
        with begin() as db:
            f = facts(request, db)
            origin = f.origin or ""
            try:
                result = sso_saml.consume_response(db, saml_response=saml_response, preauth=f.preauth,
                                                   origin=origin)
            except SsoError as exc:
                org = default_org_id(db)
                if org:
                    emit(db, org_id=org, event_type="auth.login.failed", outcome=Outcome.DENIED,
                         ip=f.ip, user_agent=f.user_agent, request_id=f.request_id,
                         details={"reason": exc.code, "protocol": "saml"})
                return _fail(origin, exc.code)
            return _redirect(origin, social.resolve(db, result.provider, result.identity, f))

    @router.post("/saml/v2/acs")
    @public(reason=PublicReason.SAML_SIGNED_ASSERTION, verifier=verify_saml_assertion)
    async def saml_acs(request: Request):
        form = await request.form()
        return await run_in_threadpool(_acs, request, str(form.get("SAMLResponse") or ""))

    # ---- provider admin ----------------------------------------------------------------------------

    @router.get("/v1/identity/providers")
    @permit(P.IDENTITY_PROVIDER_READ)
    def list_providers(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                rows = db.execute(text(f"SELECT {sso_oidc._PROVIDER_COLS} FROM identity_providers "
                                       "WHERE org_id = :o ORDER BY name"), {"o": ctx.principal.org_id})
                return JSONResponse({"providers": [_public_provider(sso_oidc.provider_from_row(r)) for r in rows]})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/providers/{provider_id}")
    @permit(P.IDENTITY_PROVIDER_READ)
    def get_provider(provider_id: str, request: Request):
        """One provider: everything an admin may see, its group mappings and its SCIM keys (ids,
        names, status, dates — never a secret or a secret reference). 404 for another org's."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None:
                    return error(404, "not_found")
                mappings = db.execute(text(
                    "SELECT m.external_group, m.group_id, g.name AS group_name FROM "
                    "identity_provider_group_mappings m LEFT JOIN identity_groups g ON g.id = m.group_id "
                    "WHERE m.provider_id = :p ORDER BY m.external_group"), {"p": provider_id}).mappings().all()
                keys = db.execute(text(
                    "SELECT id, name, created_at, expires_at, last_used_at, revoked_at, grace_until "
                    "FROM identity_api_credentials WHERE principal_id = :s AND kind = 'scim' "
                    "ORDER BY created_at DESC"), {"s": scim_principal_id(provider_id)}).mappings().all()

                def _iso(v):
                    return v.isoformat() if v else None

                def _status(k):
                    from datetime import datetime, timezone

                    now = datetime.now(timezone.utc)
                    if k["revoked_at"]:
                        return "revoked"
                    if k["grace_until"]:
                        return "grace"
                    if k["expires_at"] and k["expires_at"] <= now:
                        return "expired"
                    return "active"

                return JSONResponse({
                    **_public_provider(provider),
                    "group_mappings": [dict(m) for m in mappings],
                    "scim_keys": [{"id": k["id"], "name": k["name"], "status": _status(k),
                                   "created_at": _iso(k["created_at"]), "expires_at": _iso(k["expires_at"]),
                                   "last_used_at": _iso(k["last_used_at"])} for k in keys]})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/providers/{provider_id}/jwks")
    @permit(P.IDENTITY_PROVIDER_READ)
    def provider_client_jwks(provider_id: str, request: Request):
        """private_key_jwt: the PUBLIC key BOSS signs its client assertions with, to register at
        the IdP. Never the private key."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None or not provider.client_key_vault_item_id:
                    return error(404, "not_found")
                pem = sso_oidc._vaulted(db, provider, provider.client_key_vault_item_id)  # noqa: SLF001
                return JSONResponse({"keys": [sso_oidc.public_jwk(pem, provider.client_key_kid)]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/providers")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def create_provider(body: ProviderBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                try:
                    issuer = _resolve_issuer(body)
                    algs = _validate_algs(body.allowed_algs)
                except ValueError as exc:
                    return error(422, "invalid_provider", detail=str(exc))
                if body.protocol == "saml" and (not (body.saml_sso_url or "").startswith("https://")
                                                or not body.saml_certificates):
                    return error(422, "invalid_provider", detail="SAML needs an https SSO URL and a certificate")
                pid = body.id or ids.new_id("idp")
                org = ctx.principal.org_id
                if body.id and db.execute(text("SELECT 1 FROM identity_providers WHERE id = :i"),
                                          {"i": body.id}).first() is not None:
                    return error(409, "provider_id_taken")
                if issuer and db.execute(text(
                        "SELECT 1 FROM identity_providers WHERE org_id = :o AND lower(issuer) = lower(:i)"),
                        {"o": org, "i": issuer}).first() is not None:
                    # M1: one provider per issuer, or a second one could answer for the first's people.
                    return error(409, "issuer_in_use")
                secret_ref = _store_secret(db, org, pid, body.client_secret) if body.client_secret else None
                db.execute(text(
                    "INSERT INTO identity_providers (id, org_id, protocol, preset, name, issuer, client_id, "
                    "client_secret_vault_item_id, scopes, allowed_algs, saml_sp_entity_id, saml_certificates, "
                    "domains, jit_mode, trust_idp_mfa, enforce_sso, groups_claim, created_by, saml_sso_url, "
                    "trusted_authn_contexts) VALUES (:id, :o, "
                    ":proto, :preset, :name, :iss, :cid, :sec, :scopes, :algs, :spe, :certs, :domains, :jit, "
                    ":tmfa, :esso, :gc, :by, :sso, :tac)"),
                    {"id": pid, "o": org, "proto": body.protocol, "preset": body.preset, "name": body.name,
                     "iss": issuer, "cid": None if body.protocol == "saml" else body.client_id,
                     "sso": body.saml_sso_url if body.protocol == "saml" else None,
                     "tac": _authn_contexts(body.trusted_authn_contexts) if body.protocol == "saml" else None,
                     "sec": secret_ref, "scopes": body.scopes, "algs": algs, "spe": body.saml_sp_entity_id,
                     "certs": body.saml_certificates, "domains": [d.lower().lstrip("@") for d in body.domains],
                     "jit": body.jit_mode, "tmfa": body.trust_idp_mfa, "esso": body.enforce_sso,
                     "gc": body.groups_claim, "by": ctx.principal.id})
                try:
                    _write_mappings(db, pid, body.group_mappings, org)
                    if body.protocol == "oidc" and body.client_auth == "private_key_jwt":
                        item, kid = _client_key(db, org, pid, body.client_private_key, body.client_key_kid)
                        db.execute(text(
                            "UPDATE identity_providers SET client_auth = 'private_key_jwt', "
                            "client_key_vault_item_id = :k, client_key_kid = :kid, client_assertion_issuer = :iss "
                            "WHERE id = :id"), {"k": item, "kid": kid, "iss": body.client_assertion_issuer,
                                                "id": pid})
                except ValueError as exc:
                    raise _Rollback(error(422, "invalid_provider", detail=str(exc))) from None
                emit(db, org_id=org, event_type="sso.provider.created", outcome=Outcome.SUCCESS, ctx=ctx,
                     target_type="identity_provider", target_id=pid,
                     details={"protocol": body.protocol, "preset": body.preset})
                provider = sso_oidc.load_provider(db, pid, active_only=False)
                return JSONResponse(status_code=201, content=_public_provider(provider))
        except _Rollback as rb:
            return rb.response
        except AuthnError as exc:
            return refusal(exc)

    @router.patch("/v1/identity/providers/{provider_id}")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def update_provider(provider_id: str, body: ProviderPatch, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None:
                    return error(404, "not_found")
                fields = body.model_dump(exclude_unset=True)
                changes: dict[str, Any] = {}
                for key in ("name", "scopes", "jit_mode", "trust_idp_mfa", "enforce_sso", "groups_claim",
                            "status", "saml_certificates"):
                    if fields.get(key) is not None:
                        changes[key] = fields[key]
                if fields.get("domains") is not None:
                    changes["domains"] = [d.lower().lstrip("@") for d in fields["domains"]]
                if fields.get("allowed_algs") is not None:
                    try:
                        changes["allowed_algs"] = _validate_algs(fields["allowed_algs"])
                    except ValueError as exc:
                        return error(422, "invalid_provider", detail=str(exc))
                if provider.protocol == "saml" and fields.get("saml_sso_url"):
                    if not fields["saml_sso_url"].startswith("https://"):
                        return error(422, "invalid_provider", detail="SSO URL must be https")
                    changes["saml_sso_url"] = fields["saml_sso_url"]
                    changes["client_id"] = None          # an older row kept the SSO URL here
                elif fields.get("client_id") is not None and provider.protocol != "saml":
                    changes["client_id"] = fields["client_id"]
                if provider.protocol == "saml" and "trusted_authn_contexts" in fields:
                    changes["trusted_authn_contexts"] = _authn_contexts(fields["trusted_authn_contexts"])
                if fields.get("client_secret"):
                    changes["client_secret_vault_item_id"] = _store_secret(
                        db, provider.org_id, provider.id, fields["client_secret"])
                if provider.protocol == "oidc" and (fields.get("client_auth") == "private_key_jwt" or (
                        provider.client_auth == "private_key_jwt" and fields.get("client_private_key"))):
                    try:
                        item, kid = _client_key(db, provider.org_id, provider.id,
                                                fields.get("client_private_key"),
                                                fields.get("client_key_kid") or (
                                                    None if fields.get("client_private_key") else provider.client_key_kid))
                    except ValueError as exc:
                        return error(422, "invalid_provider", detail=str(exc))
                    if fields.get("client_private_key") or not provider.client_key_vault_item_id:
                        changes["client_key_vault_item_id"], changes["client_key_kid"] = item, kid
                    changes["client_auth"] = "private_key_jwt"
                elif fields.get("client_auth") == "client_secret":
                    changes["client_auth"] = "client_secret"
                if "client_assertion_issuer" in fields:
                    changes["client_assertion_issuer"] = fields["client_assertion_issuer"]
                for key, value in changes.items():   # keys come from the fixed lists above only
                    db.execute(text(f"UPDATE identity_providers SET {key} = :v, updated_at = now() WHERE id = :id"),
                               {"v": value, "id": provider_id})
                if fields.get("group_mappings") is not None:
                    try:
                        _write_mappings(db, provider_id, fields["group_mappings"], provider.org_id)
                    except ValueError as exc:
                        raise _Rollback(error(422, "invalid_provider", detail=str(exc))) from None
                sso_oidc.clear_caches()
                changed = sorted({"client_secret_vault_item_id": "client_secret",
                                  "client_key_vault_item_id": "client_key"}.get(k, k) for k in changes)
                emit(db, org_id=provider.org_id, event_type="sso.provider.updated", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="identity_provider", target_id=provider_id,
                     details={"changed": changed})
                return JSONResponse(_public_provider(sso_oidc.load_provider(db, provider_id, active_only=False)))
        except _Rollback as rb:
            return rb.response
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/providers/{provider_id}")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def delete_provider(provider_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None:
                    return error(404, "not_found")
                # Links stay (history); the provider is disabled, so nobody signs in through it,
                # and its SCIM keys are revoked.
                db.execute(text("UPDATE identity_providers SET status = 'disabled', updated_at = now() "
                                "WHERE id = :id"), {"id": provider_id})
                db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by "
                                "WHERE principal_id = :p AND revoked_at IS NULL"),
                           {"by": ctx.principal.id, "p": scim_principal_id(provider_id)})
                emit(db, org_id=provider.org_id, event_type="sso.provider.deleted", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="identity_provider", target_id=provider_id)
                return JSONResponse({"deleted": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/providers/{provider_id}/test")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def test_provider(provider_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None:
                    return error(404, "not_found")
            if provider.protocol == "saml":
                return JSONResponse({"ok": bool(provider.saml_certificates and provider.sso_url),
                                     "checked": "configuration"})
            sso_oidc.clear_caches()
            try:
                doc = sso_oidc.discovery(provider)
                if provider.protocol == "oidc":
                    sso_oidc._jwks(doc["jwks_uri"])
            except SsoError as exc:
                return JSONResponse({"ok": False, "error": exc.code})
            return JSONResponse({"ok": True, "checked": "discovery" if provider.protocol == "oidc" else "preset"})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/providers/{provider_id}/scim-keys")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def create_scim_key(provider_id: str, body: ScimKeyBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                provider = _own_provider(db, ctx, provider_id)
                if provider is None:
                    return error(404, "not_found")
                sid = scim_principal_id(provider_id)
                db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name, created_by) "
                                "VALUES (:id, :o, 'system', 'active', :n, :by) ON CONFLICT (id) DO NOTHING"),
                           {"id": sid, "o": provider.org_id, "n": f"SCIM ({provider.name})"[:200],
                            "by": ctx.principal.id})
                owner = users.load_principal(db, sid)
                assert owner is not None
                created = api_keys.create(db, creator=ctx, owner=owner, kind="scim", name=body.name,
                                          scopes=["scim:provision"], expires_in_days=body.expires_in_days,
                                          check_subset=False)
                return JSONResponse(status_code=201, headers={"Cache-Control": "no-store"}, content={
                    "id": created.id, "key": created.key, "principal_id": sid, "shown_once": True,
                    "expires_at": created.expires_at.isoformat() if created.expires_at else None})
        except api_keys.KeyRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/providers/{provider_id}/scim-keys/{key_id}")
    @permit(P.IDENTITY_PROVIDER_MANAGE)
    def revoke_scim_key(provider_id: str, key_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not api_keys.revoke(db, actor=ctx, key_id=key_id, org_id=ctx.principal.org_id,
                                       owner_id=scim_principal_id(provider_id)):
                    return error(404, "not_found")
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    return router
