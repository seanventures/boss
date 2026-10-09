"""'Sign in with BOSS' for an app built from the login-oauth template (F6 §6.4 "Login template apply
sheet", F3 §3.1).

One call connects the app: BOSS registers (or reuses) the app's confidential client at its OIDC
provider, registers the app's redirect URIs from where the app is actually served, and binds the
three values the template reads as app secrets, per environment, in the vault:

    OIDC_BOSS_CLIENT_ID      the client id
    OIDC_BOSS_CLIENT_SECRET  the client secret (never shown, never returned, never in a build)
    OIDC_BOSS_ISSUER         BOSS's issuer for this org (`<public origin>/oidc/<org>`)

for `preview` and `deploy` (a build never gets them: its tests use the template's fake provider).
(`BOSS_*` names are reserved for BOSS's own configuration, hence `OIDC_BOSS_*`.)

Redirect URIs (`<base>/auth/callback/boss`, the template's route) come from:
  - each running preview of the app: `<preview origin>/v1/previews/<id>/web` (an absolute origin:
    BOSS_PREVIEW_ORIGIN, else the public URL's host on BOSS_PREVIEW_PORT);
  - where the app is deployed: its newest healthy deploy's public URL and its deploy targets'
    public URL / https domains.
They are kept in step by `sync_redirect_uris`, which every preview start and stop calls: a stopped
preview's callback is removed, the new one added. URIs an operator registered by hand are kept.
With no address yet, the template's local default (`http://localhost:3000`) is registered so the
client is valid; it is dropped as soon as a real address exists.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

from . import clients as oidc_clients
from .util import audit

logger = logging.getLogger(__name__)

LOGIN_TEMPLATE = "login-oauth"
CALLBACK_PATH = "/auth/callback/boss"
LOCAL_BASE = "http://localhost:3000"
NAMES = ("OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET", "OIDC_BOSS_ISSUER")
ENVS = ("preview", "deploy")
SCOPES = ["openid", "profile", "email", "roles"]
_PREVIEW_MARK = "/v1/previews/"


class SignInError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message}


# ---- where the app is served -----------------------------------------------------------------------

def _preview_origin(db: Any, org_id: str) -> str:
    """The absolute origin browsers open previews at, or ""."""
    from ..identity.authn_support import public_origin
    from ..services.preview_service import configured_preview_origin, preview_origin_for

    configured = configured_preview_origin()
    if configured:
        return configured
    origin = public_origin(db, org_id) or ""
    if not origin:
        return ""
    parts = urlsplit(origin)
    return preview_origin_for(parts.netloc.rsplit(":", 1)[0] if parts.port else parts.netloc,
                              scheme=parts.scheme)


def preview_base(db: Any, org_id: str, preview_id: str) -> str:
    origin = _preview_origin(db, org_id)
    return f"{origin}{_PREVIEW_MARK}{preview_id}/web" if origin else ""


def _app_runs(db: Any, project_id: str, app_key: str) -> list[str]:
    from ..app_links.service import app_index

    by_run, _ = app_index(db, project_id)
    return [r for r, k in by_run.items() if k == app_key]


def preview_bases(db: Any, org_id: str, project_id: str, app_key: str,
                  also: Optional[str] = None, without: Optional[str] = None) -> list[str]:
    runs = _app_runs(db, project_id, app_key)
    ids: list[str] = []
    if runs:
        ids = [r for (r,) in db.execute(text(
            "SELECT id FROM app_previews WHERE project_id = :p AND run_id = ANY(:r) AND status = 'running' "
            "ORDER BY created_at"), {"p": project_id, "r": runs}).all()]
    if also and also not in ids:
        ids.append(also)
    return [b for b in (preview_base(db, org_id, i) for i in ids if i != without) if b]


def deploy_bases(db: Any, project_id: str, app_key: str) -> list[str]:
    from ..app_links.service import deployed_url
    from ..services.agent_assignment import app_key as key_of

    out: list[str] = []
    url = deployed_url(db, project_id, app_key)
    if url:
        out.append(url)
    for row in db.execute(text(
            "SELECT app_name, public_url, domains FROM deployment_targets WHERE project_id = :p "
            "AND status NOT IN ('deleted', 'retired')"), {"p": project_id}).mappings().all():
        if key_of(str(row["app_name"] or "")) != app_key:
            continue
        if row.get("public_url"):
            out.append(str(row["public_url"]))
        for d in (row.get("domains") or []):
            d = str(d or "").strip()
            if d and "://" not in d and ":" not in d:          # a bare domain is served over https
                out.append(f"https://{d}")
    bases = []
    for u in out:
        parts = urlsplit(u)
        if parts.scheme in ("https", "http") and parts.netloc:
            base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
            if base not in bases:
                bases.append(base)
    return bases


def _callbacks(bases: list[str]) -> list[str]:
    out: list[str] = []
    for b in bases:
        uri = f"{b.rstrip('/')}{CALLBACK_PATH}"
        try:
            oidc_clients.check_redirect_uri(uri)
        except ValueError as exc:  # e.g. a plain-http deploy that is not loopback
            logger.info("not registering %s for Sign in with BOSS: %s", uri, exc)
            continue
        if uri not in out:
            out.append(uri)
    return out


def _managed(uri: str) -> bool:
    """A callback BOSS registered for a preview, or the local placeholder: replaced on every sync."""
    return uri.endswith(CALLBACK_PATH) and (_PREVIEW_MARK in uri or uri == f"{LOCAL_BASE}{CALLBACK_PATH}")


def wanted_uris(db: Any, org_id: str, project_id: str, app_key: str, current: list[str], *,
                also: Optional[str] = None, without: Optional[str] = None) -> list[str]:
    keep = [u for u in current if not _managed(u)]
    fresh = _callbacks(preview_bases(db, org_id, project_id, app_key, also=also, without=without)
                       + deploy_bases(db, project_id, app_key))
    uris = keep + [u for u in fresh if u not in keep]
    if not uris:
        uris = [f"{LOCAL_BASE}{CALLBACK_PATH}"]
    return uris[: oidc_clients.MAX_REDIRECT_URIS]


def _logout_uris(uris: list[str]) -> list[str]:
    return [u[: -len(CALLBACK_PATH)] + "/" for u in uris if u.endswith(CALLBACK_PATH)]


# ---- the app, its template, its client --------------------------------------------------------------

def login_application(db: Any, project_id: str, app_key: str) -> Optional[dict[str, Any]]:
    row = db.execute(text(
        "SELECT a.id, a.status, a.stack, t.template_key, t.version FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id WHERE a.project_id = :p AND a.app_key = :k "
        "AND t.template_key = :t AND a.status NOT IN ('failed', 'abandoned') ORDER BY a.created_at DESC LIMIT 1"),
        {"p": project_id, "k": app_key, "t": LOGIN_TEMPLATE}).mappings().first()
    return dict(row) if row else None


def client_for_app(db: Any, org_id: str, project_id: str, app_key: str) -> Optional[dict[str, Any]]:
    row = db.execute(text(
        "SELECT id FROM oidc_clients WHERE org_id = :o AND project_id = :p AND app_key = :k "
        "AND status <> 'deleted' ORDER BY created_at DESC LIMIT 1"),
        {"o": org_id, "p": project_id, "k": app_key}).first()
    return oidc_clients.load(db, org_id=org_id, client_id=row[0], any_status=True) if row else None


def _issuer(db: Any, org_id: str) -> str:
    from .protocol import resolve
    from .util import OidcError

    try:
        return resolve(db, org_id)[1]
    except OidcError as exc:
        raise SignInError(409, "no_issuer", "BOSS has no public URL yet, so it cannot be an issuer: set "
                          "BOSS_PUBLIC_URL (or claim the install on its network origin), then try again.") from exc


def _pool(db: Any, org_id: str, pool_id: Optional[str], *, actor: str, may_create_pool: bool) -> dict[str, Any]:
    from . import pools

    if pool_id:
        pool = pools.load(db, org_id=org_id, pool_id=pool_id)
        if pool is None:
            raise SignInError(404, "pool_not_found", "no such sign-in pool in this organisation")
        return pool
    row = db.execute(text("SELECT id FROM oidc_user_pools WHERE org_id = :o AND kind = 'workforce' "
                          "ORDER BY created_at LIMIT 1"), {"o": org_id}).first()
    if row:
        return pools.load(db, org_id=org_id, pool_id=row[0])
    if not may_create_pool:
        raise SignInError(409, "no_pool", "This organisation has no sign-in pool yet. An admin creates one in "
                          "Identity › App sign-in › Pools (or connects the app themselves, which creates it).")
    return pools.create(db, org_id=org_id, actor=actor, body={"name": "Team", "kind": "workforce"})


def _display_name(db: Any, project_id: str, app_key: str) -> str:
    from ..app_links.service import app_index

    _, names = app_index(db, project_id)
    return (names.get(app_key) or app_key)[:120]


def status(db: Any, *, org_id: str, project_id: str, app_key: str) -> dict[str, Any]:
    """What the console shows: presence and addresses only, never a secret."""
    from ..templates.secrets import has_binding

    client = client_for_app(db, org_id, project_id, app_key)
    bound = {env: [n for n in NAMES if has_binding(db, project_id=project_id, app_key=app_key, name=n, env=env)]
             for env in ENVS}
    out: dict[str, Any] = {
        "app_key": app_key,
        "template_applied": login_application(db, project_id, app_key) is not None,
        "connected": bool(client and client["status"] == "active"
                          and all(len(bound[e]) == len(NAMES) for e in ENVS)),
        "secrets": bound,
        "client": None,
    }
    if client:
        out["client"] = {"client_id": client["id"], "status": client["status"], "pool_id": client["pool_id"],
                         "redirect_uris": client["redirect_uris"], "allowed_scopes": client["allowed_scopes"]}
        try:
            out["issuer"] = _issuer(db, org_id)
        except SignInError:
            out["issuer"] = None
    return out


def connect(db: Any, *, org_id: str, project_id: str, app_key: str, actor: str,
            pool_id: Optional[str] = None, may_create_pool: bool = False) -> dict[str, Any]:
    """Register (or reuse) the app's client, sync its redirect URIs, bind the three values for
    preview and deploy. Idempotent: a second call rotates nothing that is already bound."""
    from ..templates.secrets import bind_issued, has_binding

    if login_application(db, project_id, app_key) is None:
        raise SignInError(409, "login_template_not_applied",
                          f"Apply the {LOGIN_TEMPLATE} template to this app first: Sign in with BOSS fills its "
                          "OIDC_BOSS_* settings.")
    issuer = _issuer(db, org_id)
    client = client_for_app(db, org_id, project_id, app_key)
    secret: Optional[str] = None
    rotated = created = False
    if client is None:
        pool = _pool(db, org_id, pool_id, actor=actor, may_create_pool=may_create_pool)
        uris = wanted_uris(db, org_id, project_id, app_key, [])
        client, secret = oidc_clients.create(db, org_id=org_id, actor=actor, body={
            "project_id": project_id, "app_key": app_key, "pool_id": pool["id"],
            "name": _display_name(db, project_id, app_key), "type": "confidential",
            "token_endpoint_auth_method": "client_secret_basic", "redirect_uris": uris,
            "post_logout_redirect_uris": _logout_uris(uris), "allowed_scopes": SCOPES})
        created = True
    else:
        if pool_id and client["pool_id"] != pool_id:
            raise SignInError(409, "pool_differs", "This app is already a client of another pool; remove it in "
                              "Identity › App sign-in to move it.")
        if client["token_endpoint_auth_method"] != "client_secret_basic":
            raise SignInError(409, "not_a_secret_client", "This app's client does not use a client secret; the "
                              "login template needs one (client_secret_basic).")
        updates: dict[str, Any] = {}
        if client["status"] != "active":
            updates["status"] = "active"
        uris = wanted_uris(db, org_id, project_id, app_key, client["redirect_uris"])
        if uris != client["redirect_uris"]:
            updates.update(redirect_uris=uris, post_logout_redirect_uris=_merge_logout(client, uris))
        if updates:
            client = oidc_clients.update(db, org_id=org_id, actor=actor, client_id=client["id"], body=updates)
        if not all(has_binding(db, project_id=project_id, app_key=app_key, name="OIDC_BOSS_CLIENT_SECRET", env=e)
                   for e in ENVS):
            # The secret was shown once when someone registered the client by hand: BOSS cannot
            # read it back, so it issues a new one (the old one stops working now).
            client, secret = oidc_clients.rotate_secret(db, org_id=org_id, actor=actor, client_id=client["id"])
            rotated = True
    values = {"OIDC_BOSS_CLIENT_ID": client["id"], "OIDC_BOSS_ISSUER": issuer}
    if secret is not None:
        values["OIDC_BOSS_CLIENT_SECRET"] = secret
    for env in ENVS:
        for name, value in values.items():
            bind_issued(db, org_id=org_id, project_id=project_id, app_key=app_key, name=name, env=env,
                        value=value, actor_id=actor)
    audit(db, org_id=org_id, event_type="oidc.client.updated", actor_principal_id=actor, actor_kind="user",
          target_type="oidc_client", target_id=client["id"],
          details={"sign_in_with_boss": "connected", "app_key": app_key, "created": created,
                   "reissued": rotated, "envs": list(ENVS)})
    return {**status(db, org_id=org_id, project_id=project_id, app_key=app_key),
            "created": created, "secret_rotated": rotated}


def _merge_logout(client: dict[str, Any], uris: list[str]) -> list[str]:
    keep = [u for u in client.get("post_logout_redirect_uris") or []
            if not (_PREVIEW_MARK in u or u == f"{LOCAL_BASE}/")]
    return (keep + [u for u in _logout_uris(uris) if u not in keep])[: oidc_clients.MAX_REDIRECT_URIS]


def sync_redirect_uris(db: Any, *, org_id: str, project_id: str, app_key: str, also: Optional[str] = None,
                       without: Optional[str] = None, actor: str = "boss") -> Optional[list[str]]:
    """Bring the app's client's redirect URIs up to where the app is served now. None when the app
    has no client (nothing to do)."""
    client = client_for_app(db, org_id, project_id, app_key)
    if client is None or client["status"] == "deleted":
        return None
    uris = wanted_uris(db, org_id, project_id, app_key, client["redirect_uris"], also=also, without=without)
    if uris != client["redirect_uris"]:
        oidc_clients.update(db, org_id=org_id, actor=actor, client_id=client["id"],
                            body={"redirect_uris": uris, "post_logout_redirect_uris": _merge_logout(client, uris)})
    return uris


def disconnect(db: Any, *, org_id: str, project_id: str, app_key: str, actor: str) -> dict[str, Any]:
    """Remove the app's client (its grants end) and its three bindings."""
    from ..templates.secrets import delete as delete_binding
    from ..templates.service import TemplateError

    client = client_for_app(db, org_id, project_id, app_key)
    if client is None:
        raise SignInError(404, "not_connected", "This app does not use Sign in with BOSS.")
    oidc_clients.delete(db, org_id=org_id, actor=actor, client_id=client["id"])
    for env in ENVS:
        for name in NAMES:
            try:
                delete_binding(db, project_id=project_id, app_key=app_key, name=name, env=env)
            except TemplateError:
                pass
    return status(db, org_id=org_id, project_id=project_id, app_key=app_key)


# ---- a preview's environment -----------------------------------------------------------------------

def preview_environment(db: Any, *, project_id: str, run_id: Optional[str], preview_id: str) -> dict[str, str]:
    """What a preview of an app gets from its templates (F2 §2.4): its `preview` secret values
    (test/generated/stub, never live), APP_BASE_URL (where the browser reaches it), and, when the
    app uses Sign in with BOSS, its redirect URI registered for this preview."""
    from ..app_links.service import app_index, project_org
    from ..templates.secrets import values_for_env

    if not run_id:
        return {}
    by_run, _ = app_index(db, project_id)
    app_key = by_run.get(run_id)
    org_id = project_org(db, project_id)
    if not app_key or not org_id:
        return {}
    applied = db.execute(text(
        "SELECT 1 FROM app_template_applications WHERE project_id = :p AND app_key = :k "
        "AND status NOT IN ('failed', 'abandoned') LIMIT 1"), {"p": project_id, "k": app_key}).first()
    if not applied:             # an app no template was applied to keeps its environment as it was
        return {}
    env, withheld = values_for_env(db, org_id=org_id, project_id=project_id, app_key=app_key, env="preview")
    for why in withheld:
        logger.warning("preview %s: %s", preview_id, why)
    base = preview_base(db, org_id, preview_id)
    if base:
        env.setdefault("APP_BASE_URL", base)
    sync_redirect_uris(db, org_id=org_id, project_id=project_id, app_key=app_key, also=preview_id)
    if env:
        from ..identity import redaction

        redaction.register_secrets([v for k, v in env.items() if k != "APP_BASE_URL"])
    return env


def preview_stopped(db: Any, *, project_id: str, run_id: Optional[str], preview_id: str) -> None:
    from ..app_links.service import app_index, project_org

    if not run_id:
        return
    by_run, _ = app_index(db, project_id)
    app_key = by_run.get(run_id)
    org_id = project_org(db, project_id)
    if app_key and org_id:
        sync_redirect_uris(db, org_id=org_id, project_id=project_id, app_key=app_key, without=preview_id)
