"""Where an app built from templates is reached, for the templates that need to know.

A template that builds absolute URLs (an OAuth redirect URI, a payment return URL) declares the
well-known env `APP_BASE_URL` (`env: [{name: APP_BASE_URL, default: <the app's local URL>}]`).
BOSS fills it wherever the app runs, from the address browsers and providers actually reach:

    preview   the preview's gateway URL     (oidc_provider.app_sign_in.preview_environment)
    deploy    the deployment target's public address (`deploy_environment`, below)

Left at the template default, a deployed app built its OAuth callbacks on its local URL.
`deploy_environment` is THE reader a deploy uses for the app's environment: the app's `deploy`
secrets plus the base URL. A value the operator bound by hand under that name wins.

Callback URLs: a template with a route `GET <path>/:provider` whose path has `callback` in it,
and secrets `<PROVIDER>_CLIENT_ID`, gets one callback per provider (`OIDC_BOSS_CLIENT_ID` is the
provider `boss`): `<base><path>/<provider>`. That is what the operator registers at the provider.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

#: The env name a template declares to be told its public base URL.
BASE_URL_ENV = "APP_BASE_URL"

_CALLBACK_ROUTE = re.compile(r"^GET\s+(/\S*callback\S*)/:provider$", re.IGNORECASE)


def _manifests(c, project_id: str, app_key: str) -> list[dict]:
    rows = c.execute(text(
        "SELECT t.manifest FROM app_template_applications a JOIN app_templates t ON t.id = a.template_id "
        "WHERE a.project_id = :p AND a.app_key = :k AND a.status NOT IN ('failed', 'abandoned') "
        "ORDER BY a.created_at"), {"p": project_id, "k": app_key}).all()
    out = []
    for (m,) in rows:
        if isinstance(m, str):
            m = json.loads(m)
        if isinstance(m, dict):
            out.append(m)
    return out


def declares_base_url(manifest: dict) -> bool:
    return any(isinstance(e, dict) and e.get("name") == BASE_URL_ENV for e in manifest.get("env") or [])


def uses_base_url(c, project_id: str, app_key: str) -> bool:
    """Does any template applied to the app declare APP_BASE_URL?"""
    return any(declares_base_url(m) for m in _manifests(c, project_id, app_key))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    return ""


def target_base_url(target: dict) -> str:
    """The app's public base URL on a deployment target, no trailing slash ("" when it has none).

    The target's configured public URL wins; then its domain (a scheme-less `host:port` is plain
    http, as the SSH connector publishes it; a bare domain is https behind a proxy); then the
    host of its SSH ref on the app's port. A domain always names the app port, as the connector
    publishes and rewrites it (deployment_connectors.domain_with_port); a bare domain with no
    known port is https behind a proxy."""
    from ..services.deployment_connectors import _app_port, _split_ref, domain_with_port

    public = _origin(str(target.get("public_url") or "").strip())
    if public:
        return public
    port = _app_port(target, default=0)
    domains = target.get("domains") or []
    if isinstance(domains, str):
        try:
            domains = json.loads(domains)
        except ValueError:
            domains = [domains]
    domain = str((domains or [""])[0] or "").strip()
    if "://" in domain:
        return _origin(domain)
    domain = domain.split("/")[0]
    try:
        _user, host, _ssh = _split_ref(str(target.get("target_ref") or ""))
    except Exception:  # noqa: BLE001 — a ref BOSS cannot read gives no host
        host = ""
    if domain and ":" not in domain and not port:
        return f"https://{domain}"
    if port:
        fixed = domain_with_port(domain or None, host, port)
        domain = fixed or domain
    return f"http://{domain}" if domain else ""


def _callback_paths(manifest: dict) -> list[str]:
    return [mo.group(1) for r in manifest.get("routes") or [] if (mo := _CALLBACK_ROUTE.match(str(r).strip()))]


def sign_in_providers(manifest: dict) -> list[dict[str, str]]:
    """The sign-in providers a template wires: [{provider, id, secret}], one per
    `<PROVIDER>_CLIENT_ID` secret (`OIDC_BOSS_CLIENT_ID` is `boss`) of a template with a route
    `GET <path with callback>/:provider`; `id` and `secret` are the names the app reads. A template
    with a `_CLIENT_ID` and no callback route (a payment API's client) wires no sign-in provider."""
    if not _callback_paths(manifest):
        return []
    out: list[dict[str, str]] = []
    for s in manifest.get("secrets") or []:
        name = str((s or {}).get("name") or "") if isinstance(s, dict) else ""
        if not name.endswith("_CLIENT_ID"):
            continue
        p = name[: -len("_CLIENT_ID")].lower()
        p = p[len("oidc_"):] if p.startswith("oidc_") else p
        if p and p not in [o["provider"] for o in out]:
            out.append({"provider": p, "id": name, "secret": name[: -len("_ID")] + "_SECRET"})
    return out


def callbacks(manifests: list[dict], base: str) -> list[dict[str, str]]:
    """[{provider, url}] the operator registers at each sign-in provider, for `base`."""
    out: list[dict[str, str]] = []
    if not base:
        return out
    for m in manifests:
        for path in _callback_paths(m):
            for p in sign_in_providers(m):
                item = {"provider": p["provider"], "url": f"{base.rstrip('/')}{path}/{p['provider']}"}
                if item not in out:
                    out.append(item)
    return out


def app_targets(c, project_id: str, app_key: str) -> list[dict]:
    """The app's live deployment targets (by the app key its name files under)."""
    from ..services.agent_assignment import app_key as key_of

    rows = c.execute(text(
        "SELECT * FROM deployment_targets WHERE project_id = :p "
        "AND status NOT IN ('deleted', 'retired') ORDER BY created_at"), {"p": project_id}).mappings().all()
    return [dict(r) for r in rows if key_of(str(r.get("app_name") or "")) == app_key]


def deploy_address(c, project_id: str, app_key: str, target: Optional[dict] = None) -> dict[str, Any]:
    """{base_url, callbacks} for the app deployed on `target` (or its first target). Empty when
    no template of the app declares APP_BASE_URL or it has no address yet."""
    manifests = _manifests(c, project_id, app_key)
    if not any(declares_base_url(m) for m in manifests):
        return {}
    targets = [target] if target else app_targets(c, project_id, app_key)
    base = next((b for b in (target_base_url(t) for t in targets) if b), "")
    if not base:
        return {}
    return {"env": BASE_URL_ENV, "base_url": base, "callbacks": callbacks(manifests, base)}


def deploy_secret_values(c, *, org_id: str, project_id: str,
                         app_key: str) -> tuple[dict[str, str], list[str]]:
    """THE ONE READ of an app's `deploy` secret values, for every deploy-side caller: the app's
    environment (deploy_environment, below) and a platform resource's bindings
    (platform.bindings, platform.executor). Values never leave the caller."""
    from .secrets import values_for_env

    return values_for_env(c, org_id=org_id, project_id=project_id, app_key=app_key, env="deploy")


def deploy_environment(c, *, org_id: Optional[str], project_id: str, app_key: str,
                       target: dict) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """What a deploy of the app on `target` puts in the app's environment (.boss/app.env): its
    `deploy` secret values, plus APP_BASE_URL when a template declares it. Returns
    (env, withheld, address) — `address` is {base_url, callbacks} for the deploy's record."""
    env: dict[str, str] = {}
    withheld: list[str] = []
    if org_id:
        env, withheld = deploy_secret_values(c, org_id=org_id, project_id=project_id, app_key=app_key)
    address = deploy_address(c, project_id, app_key, target)
    if address:
        env.setdefault(BASE_URL_ENV, address["base_url"])
        if env[BASE_URL_ENV] != address["base_url"]:      # the operator's own value wins, and is shown
            address = {**address, "base_url": env[BASE_URL_ENV],
                       "callbacks": callbacks(_manifests(c, project_id, app_key), env[BASE_URL_ENV])}
    return env, withheld, address


def address_lines(address: dict[str, Any]) -> str:
    """The deploy record's words: the base URL and each callback to register."""
    if not address:
        return ""
    lines = [f"{address.get('env', BASE_URL_ENV)}={address['base_url']}"]
    for cb in address.get("callbacks") or []:
        lines.append(f"Callback URL for {cb['provider']}: {cb['url']}")
    return "\n".join(lines)
