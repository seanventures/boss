"""First-class IdP product packs for the top enterprise identity providers.

This catalog sits on top of the generic OIDC / SAML / SCIM runtime
(``enterprise.py``, ``oidc.py``, ``saml.py``). It does **not** reimplement
crypto or protocol handlers — it provides:

* market-validated product catalog (top 10 IdPs)
* setup blueprints and claim maps
* metadata URL templates
* dry-run materialization of enterprise IdP config payloads

Secrets never live in the catalog. Blueprint materialization emits
``secret_ref`` placeholders only.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, Optional

IdpStatus = Literal["available", "planned"]
Protocol = Literal["oidc", "saml", "scim"]

# Default OIDC claim map used when an IdP does not publish a stronger mapping.
_DEFAULT_OIDC_CLAIM_MAP: dict[str, str] = {
    "sub": "subject",
    "email": "email",
    "preferred_username": "username",
    "name": "display_name",
    "given_name": "given_name",
    "family_name": "family_name",
    "groups": "groups",
}


def _entry(
    *,
    id: str,
    name: str,
    category: str,
    protocol_support: list[str],
    market_notes: str,
    stars: float,
    setup_steps: list[str],
    default_oidc_claim_map: Optional[dict[str, str]] = None,
    metadata_url_templates: Optional[dict[str, str]] = None,
    docs_url: str,
    status: IdpStatus = "available",
    primary_protocol: str = "oidc",
    config_fields: Optional[list[dict[str, Any]]] = None,
    enterprise_provider_type_default: str = "oidc",
) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "category": category,
        "protocol_support": list(protocol_support),
        "primary_protocol": primary_protocol,
        "stars": stars,
        "market_notes": market_notes,
        "setup_steps": list(setup_steps),
        "default_oidc_claim_map": dict(
            default_oidc_claim_map or _DEFAULT_OIDC_CLAIM_MAP
        ),
        "metadata_url_templates": dict(metadata_url_templates or {}),
        "docs_url": docs_url,
        "status": status,
        "config_fields": list(config_fields or []),
        "enterprise_provider_type_default": enterprise_provider_type_default,
    }


IDENTITY_PROVIDER_CATALOG: list[dict[str, Any]] = [
    _entry(
        id="okta",
        name="Okta",
        category="workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.8,
        market_notes=(
            "Leading workforce IAM; most common enterprise SSO for SaaS. "
            "Strong OIDC + SCIM 2.0 provisioning coverage."
        ),
        setup_steps=[
            "Create an OIDC Web application in the Okta Admin Console.",
            "Set Sign-in redirect URI to BOSS ACS / callback URL.",
            "Copy Client ID; store Client Secret as a BOSS secret_ref.",
            "Note the Org Authorization Server issuer "
            "(https://{domain}/oauth2/default or custom).",
            "Optionally enable SCIM provisioning with a BOSS SCIM bearer token.",
            "Map groups claim if role-based access is required.",
            "Register the IdP in BOSS (status=planned), test login, then activate.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "preferred_username": "username",
            "name": "display_name",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://{domain}/oauth2/default",
            "oidc_discovery": "https://{domain}/oauth2/default/.well-known/openid-configuration",
            "jwks_url": "https://{domain}/oauth2/default/v1/keys",
            "saml_metadata": "https://{domain}/app/{app_id}/sso/saml/metadata",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://developer.okta.com/docs/guides/sign-into-web-app-redirect/main/",
        config_fields=[
            {"key": "domain", "label": "Okta domain", "placeholder": "dev-123.okta.com", "required": True},
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "Audience / API identifier", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:okta-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="entra_id",
        name="Microsoft Entra ID",
        category="workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.7,
        market_notes=(
            "Formerly Azure AD. Dominant enterprise IdP for Microsoft 365 tenants; "
            "excellent OIDC, SAML federation, and SCIM enterprise app provisioning."
        ),
        setup_steps=[
            "Register a multi-tenant or single-tenant app in Entra ID → App registrations.",
            "Add a Web redirect URI for the BOSS OIDC callback.",
            "Create a client secret and store it as secret_ref (never in BOSS config JSON).",
            "Expose an API / set Application ID URI as audience when using access tokens.",
            "Copy Tenant ID; issuer becomes https://login.microsoftonline.com/{tenant_id}/v2.0.",
            "Optional: assign Enterprise Application and enable SCIM provisioning to BOSS.",
            "Upsert IdP metadata in BOSS, validate token claims, then set status=active.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "oid": "entra_object_id",
            "preferred_username": "username",
            "email": "email",
            "name": "display_name",
            "roles": "groups",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://login.microsoftonline.com/{tenant_id}/v2.0",
            "oidc_discovery": "https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
            "jwks_url": "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            "saml_metadata": "https://login.microsoftonline.com/{tenant_id}/federationmetadata/2007-06/federationmetadata.xml",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc",
        config_fields=[
            {"key": "tenant_id", "label": "Directory (tenant) ID", "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "required": True},
            {"key": "client_id", "label": "Application (client) ID", "required": True},
            {"key": "audience", "label": "Audience / Application ID URI", "placeholder": "api://boss", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:entra-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="google_workspace",
        name="Google Workspace",
        category="workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.6,
        market_notes=(
            "Default IdP for Google Workspace organizations. OIDC via Google Identity; "
            "SAML SSO for third-party apps; SCIM via Directory API / partner connectors."
        ),
        setup_steps=[
            "Create an OAuth 2.0 Web client in Google Cloud Console (APIs & Services).",
            "Configure authorized redirect URIs for BOSS.",
            "Restrict login to your Workspace domain (hd claim / hosted domain).",
            "Store client secret as secret_ref.",
            "For SAML apps, use Admin Console → Apps → Web and mobile apps.",
            "Optional SCIM: use a supported provisioning partner or Directory API bridge.",
            "Register issuer accounts.google.com and JWKS in BOSS.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "email_verified": "email_verified",
            "name": "display_name",
            "hd": "hosted_domain",
            "picture": "avatar_url",
        },
        metadata_url_templates={
            "issuer": "https://accounts.google.com",
            "oidc_discovery": "https://accounts.google.com/.well-known/openid-configuration",
            "jwks_url": "https://www.googleapis.com/oauth2/v3/certs",
            "saml_metadata": "https://admin.google.com/ac/apps/unified",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://developers.google.com/identity/openid-connect/openid-connect",
        config_fields=[
            {"key": "client_id", "label": "OAuth client ID", "required": True},
            {"key": "hosted_domain", "label": "Workspace domain (hd)", "placeholder": "example.com", "required": False},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:google-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="auth0",
        name="Auth0",
        category="customer_and_workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.5,
        market_notes=(
            "Popular developer-friendly CIAM/workforce platform (Okta brand). "
            "First-class OIDC; SAML and SCIM via Enterprise Connections / extensions."
        ),
        setup_steps=[
            "Create a Regular Web Application in the Auth0 dashboard.",
            "Set Allowed Callback URLs to the BOSS OIDC callback.",
            "Copy Domain, Client ID; store Client Secret as secret_ref.",
            "Issuer is https://{domain}/; enable RS256.",
            "Add an API audience if access tokens are validated by BOSS.",
            "Optional: configure SAML IdP or SCIM connector for lifecycle management.",
            "Register and test in BOSS before enabling auth_allowed rollout.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "nickname": "username",
            "name": "display_name",
            "https://boss.example/groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://{domain}/",
            "oidc_discovery": "https://{domain}/.well-known/openid-configuration",
            "jwks_url": "https://{domain}/.well-known/jwks.json",
            "saml_metadata": "https://{domain}/samlp/metadata/{client_id}",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://auth0.com/docs/authenticate/protocols/openid-connect-protocol",
        config_fields=[
            {"key": "domain", "label": "Auth0 domain", "placeholder": "tenant.us.auth0.com", "required": True},
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "API audience", "placeholder": "https://boss.example/api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:auth0-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="keycloak",
        name="Keycloak",
        category="self_hosted_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.4,
        market_notes=(
            "Leading open-source IdP. Preferred for self-hosted / air-gapped BOSS "
            "deployments. Full OIDC + SAML; SCIM via extensions or custom mappers."
        ),
        setup_steps=[
            "Create or select a Realm in Keycloak Admin Console.",
            "Create an OpenID Connect client (confidential) for BOSS.",
            "Set Valid Redirect URIs and Web Origins.",
            "Issuer is typically {base_url}/realms/{realm}.",
            "Copy client_id; store client secret as secret_ref.",
            "Configure protocol mappers for email, groups, and roles.",
            "Optional SAML client or SCIM provider for provisioning.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "preferred_username": "username",
            "name": "display_name",
            "realm_access.roles": "roles",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "{base_url}/realms/{realm}",
            "oidc_discovery": "{base_url}/realms/{realm}/.well-known/openid-configuration",
            "jwks_url": "{base_url}/realms/{realm}/protocol/openid-connect/certs",
            "saml_metadata": "{base_url}/realms/{realm}/protocol/saml/descriptor",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://www.keycloak.org/docs/latest/securing_apps/",
        config_fields=[
            {"key": "base_url", "label": "Keycloak base URL", "placeholder": "https://keycloak.example", "required": True},
            {"key": "realm", "label": "Realm", "placeholder": "boss", "required": True},
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:keycloak-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="github",
        name="GitHub",
        category="developer_idp",
        protocol_support=["oidc"],
        stars=4.3,
        market_notes=(
            "Developer identity for OSS and engineering orgs. OIDC (OAuth App / GitHub App). "
            "No native SAML IdP role for third parties; Enterprise Cloud uses SAML as SP."
        ),
        setup_steps=[
            "Create an OAuth App or GitHub App under Organization settings.",
            "Set Authorization callback URL to BOSS callback.",
            "Copy Client ID; store Client Secret as secret_ref.",
            "Issuer for GitHub user OIDC tokens is https://github.com/login/oauth "
            "(or use device/user flows via API).",
            "Prefer organization membership checks for access control.",
            "Register in BOSS as OIDC metadata-only first, then auth_allowed.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "login": "username",
            "email": "email",
            "name": "display_name",
            "id": "github_user_id",
        },
        metadata_url_templates={
            "issuer": "https://token.actions.githubusercontent.com",
            "oidc_discovery": "https://token.actions.githubusercontent.com/.well-known/openid-configuration",
            "jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
        },
        docs_url="https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",
        config_fields=[
            {"key": "client_id", "label": "OAuth / GitHub App client ID", "required": True},
            {"key": "audience", "label": "Audience (Actions OIDC)", "placeholder": "boss", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:github-oauth-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="gitlab",
        name="GitLab",
        category="developer_idp",
        protocol_support=["oidc", "saml"],
        stars=4.2,
        market_notes=(
            "Self-managed and GitLab.com identity for DevSecOps orgs. "
            "OIDC OmniAuth provider; SAML for Enterprise Edition SSO."
        ),
        setup_steps=[
            "In GitLab Admin → Applications (or Group Applications), create an OAuth app.",
            "Enable openid / profile / email scopes.",
            "Set redirect URI to BOSS callback.",
            "Issuer is typically {base_url} (e.g. https://gitlab.com).",
            "Store application secret as secret_ref.",
            "For EE SAML SSO, configure the group SAML IdP metadata if GitLab is SP; "
            "when GitLab is IdP, use the instance SAML endpoints.",
            "Register in BOSS and validate claims.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "nickname": "username",
            "name": "display_name",
            "groups_direct": "groups",
        },
        metadata_url_templates={
            "issuer": "{base_url}",
            "oidc_discovery": "{base_url}/.well-known/openid-configuration",
            "jwks_url": "{base_url}/oauth/discovery/keys",
            "authorize_url": "{base_url}/oauth/authorize",
            "token_url": "{base_url}/oauth/token",
        },
        docs_url="https://docs.gitlab.com/ee/integration/openid_connect_provider.html",
        config_fields=[
            {"key": "base_url", "label": "GitLab base URL", "placeholder": "https://gitlab.com", "required": True},
            {"key": "client_id", "label": "Application ID", "required": True},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Secret ref", "placeholder": "secretref:gitlab-app-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="onelogin",
        name="OneLogin",
        category="workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.1,
        market_notes=(
            "Established workforce IAM (One Identity). Solid SAML heritage with modern "
            "OIDC apps and SCIM provisioning connectors."
        ),
        setup_steps=[
            "Create an OpenID Connect app in OneLogin Administration.",
            "Configure Login Redirect URL for BOSS.",
            "Copy Client ID and Issuer URL; store Client Secret as secret_ref.",
            "Enable groups / roles parameters as needed.",
            "Optional: add SCIM connector pointing at BOSS SCIM base URL.",
            "For SAML, use the app SSO tab metadata URL.",
            "Register in BOSS, dry-run blueprint, then activate after login test.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "preferred_username": "username",
            "name": "display_name",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://{subdomain}.onelogin.com/oidc/2",
            "oidc_discovery": "https://{subdomain}.onelogin.com/oidc/2/.well-known/openid-configuration",
            "jwks_url": "https://{subdomain}.onelogin.com/oidc/2/certs",
            "saml_metadata": "https://{subdomain}.onelogin.com/saml/metadata/{app_id}",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://developers.onelogin.com/openid-connect",
        config_fields=[
            {"key": "subdomain", "label": "OneLogin subdomain", "placeholder": "acme", "required": True},
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:onelogin-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="jumpcloud",
        name="JumpCloud",
        category="workforce_idp",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.0,
        market_notes=(
            "Cloud directory popular with mid-market. OpenID Connect apps, SAML SSO, "
            "and SCIM identity management for SaaS."
        ),
        setup_steps=[
            "In JumpCloud Admin Portal, create an SSO Application (OIDC).",
            "Set Redirect URI to BOSS callback.",
            "Copy Client ID and endpoints; store Client Secret as secret_ref.",
            "Assign user groups that should access BOSS.",
            "Optional SCIM: enable identity management and point to BOSS SCIM URL.",
            "Register IdP metadata in BOSS as planned → test → active.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "preferred_username": "username",
            "name": "display_name",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://oauth.id.jumpcloud.com/",
            "oidc_discovery": "https://oauth.id.jumpcloud.com/.well-known/openid-configuration",
            "jwks_url": "https://oauth.id.jumpcloud.com/.well-known/jwks.json",
            "saml_metadata": "https://console.jumpcloud.com/#/sso/{app_id}/details",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://jumpcloud.com/support/sso-with-oidc",
        config_fields=[
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:jumpcloud-client-secret", "required": False, "secret": True},
        ],
    ),
    _entry(
        id="ping",
        name="PingFederate / PingOne",
        category="enterprise_federation",
        protocol_support=["oidc", "saml", "scim"],
        stars=4.0,
        market_notes=(
            "Ping Identity (PingFederate self-hosted + PingOne cloud). Common in "
            "large enterprises and federated B2B. Mature SAML; strong OIDC and SCIM."
        ),
        setup_steps=[
            "Create an OIDC client in PingOne or a PingFederate OAuth client.",
            "Configure redirect URIs for BOSS.",
            "Copy environment / issuer ID; store client secret as secret_ref.",
            "For PingFederate, publish metadata at the server's OIDC discovery path.",
            "Map attributes for email, sub, and group membership.",
            "Optional SCIM provisioning to BOSS SCIM endpoint.",
            "Register in BOSS and exercise fail-closed rollout modes.",
        ],
        default_oidc_claim_map={
            "sub": "subject",
            "email": "email",
            "preferred_username": "username",
            "name": "display_name",
            "memberOf": "groups",
            "groups": "groups",
        },
        metadata_url_templates={
            "issuer": "https://auth.pingone.com/{env_id}/as",
            "oidc_discovery": "https://auth.pingone.com/{env_id}/as/.well-known/openid-configuration",
            "jwks_url": "https://auth.pingone.com/{env_id}/as/jwks",
            "saml_metadata": "https://auth.pingone.com/{env_id}/saml20/metadata/{app_id}",
            "pingfederate_discovery": "{base_url}/.well-known/openid-configuration",
            "scim_base_url": "https://boss.example/scim/v2/{project_id}",
        },
        docs_url="https://docs.pingidentity.com/pingone/applications/p1_oidc_applications.html",
        config_fields=[
            {"key": "env_id", "label": "PingOne environment ID", "placeholder": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "required": False},
            {"key": "base_url", "label": "PingFederate base URL (self-hosted)", "placeholder": "https://ping.example.com", "required": False},
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "audience", "label": "Audience", "placeholder": "boss-api", "required": False},
            {"key": "secret_ref", "label": "Client secret ref", "placeholder": "secretref:ping-client-secret", "required": False, "secret": True},
        ],
    ),
]


_CATALOG_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: item for item in IDENTITY_PROVIDER_CATALOG
}


def list_identity_provider_catalog() -> list[dict[str, Any]]:
    """Return a deep copy of the static IdP catalog (no secrets)."""
    return deepcopy(IDENTITY_PROVIDER_CATALOG)


def get_identity_provider(idp_id: str) -> Optional[dict[str, Any]]:
    """Return one catalog entry by id, or None if unknown."""
    entry = _CATALOG_BY_ID.get((idp_id or "").strip().lower())
    return deepcopy(entry) if entry else None


def catalog_summary() -> dict[str, Any]:
    """Lightweight list payload for GET /v1/identity-providers."""
    items = []
    for entry in IDENTITY_PROVIDER_CATALOG:
        items.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "category": entry["category"],
                "protocol_support": list(entry["protocol_support"]),
                "primary_protocol": entry["primary_protocol"],
                "stars": entry["stars"],
                "market_notes": entry["market_notes"],
                "docs_url": entry["docs_url"],
                "status": entry["status"],
            }
        )
    return {
        "version": 1,
        "product": "boss_identity_providers",
        "count": len(items),
        "items": items,
        "standards": [
            "OpenID Connect 1.0",
            "SAML 2.0",
            "SCIM 2.0",
        ],
        "runtime": {
            "oidc": "boss_engine.services.oidc",
            "saml": "boss_engine.services.saml",
            "enterprise_store": "boss_engine.services.enterprise",
        },
    }


def _format_templates(
    templates: dict[str, str],
    variables: dict[str, str],
) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for key, template in templates.items():
        try:
            rendered[key] = template.format_map(_SafeDict(variables))
        except Exception:
            # Leave unresolved templates out rather than failing closed mid-render
            # for optional fields (e.g. PingFederate vs PingOne).
            if "{" not in template:
                rendered[key] = template
    return rendered


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _pick_provider_type(entry: dict[str, Any], requested: Optional[str]) -> str:
    if requested:
        normalized = requested.strip().lower()
        if normalized in entry["protocol_support"]:
            return normalized
        raise ValueError(
            f"Protocol '{requested}' is not supported by {entry['id']}. "
            f"Supported: {', '.join(entry['protocol_support'])}"
        )
    return str(entry.get("enterprise_provider_type_default") or entry["primary_protocol"])


def materialize_blueprint(
    idp_id: str,
    *,
    project_id: str,
    variables: Optional[dict[str, Any]] = None,
    provider_type: Optional[str] = None,
    display_name: Optional[str] = None,
    audience: Optional[str] = None,
    secret_ref: Optional[str] = None,
    status: str = "planned",
    actor: str = "system",
) -> dict[str, Any]:
    """Materialize a dry-run enterprise IdP config payload from a catalog entry.

    Returns a blueprint that can be POSTed to
    ``/v1/enterprise/identity-providers`` after operator review.
    Never embeds raw secrets — only optional ``secret_ref`` names.
    """
    entry = get_identity_provider(idp_id)
    if not entry:
        raise KeyError(f"Unknown identity provider: {idp_id}")
    if entry["status"] != "available":
        raise ValueError(
            f"Identity provider '{idp_id}' is status={entry['status']}; "
            "blueprint materialization is only allowed for available packs."
        )

    raw_vars = dict(variables or {})
    # Coerce all variables to strings for template substitution.
    string_vars: dict[str, str] = {
        str(k): str(v).strip()
        for k, v in raw_vars.items()
        if v is not None and str(v).strip()
    }
    string_vars.setdefault("project_id", project_id)

    # Fail closed on required config fields.
    missing = [
        field["key"]
        for field in entry.get("config_fields") or []
        if field.get("required") and field["key"] not in string_vars
    ]
    # env_id OR base_url acceptable for Ping; special-case handled below.
    if idp_id == "ping":
        if not string_vars.get("env_id") and not string_vars.get("base_url"):
            if "env_id" not in missing:
                missing.append("env_id_or_base_url")
        missing = [m for m in missing if m not in ("env_id", "base_url")]

    if missing:
        raise ValueError(
            f"Missing required blueprint fields for {idp_id}: {', '.join(missing)}"
        )

    # Reject secret-looking values in variables (fail closed — no secrets in payload).
    secret_keys = {
        "client_secret",
        "secret",
        "password",
        "private_key",
        "api_token",
        "token",
        "client_secret_value",
    }
    leaked = [k for k in string_vars if k.lower() in secret_keys]
    if leaked:
        raise ValueError(
            "Blueprint variables must not include raw secrets "
            f"({', '.join(sorted(leaked))}); use secret_ref instead."
        )

    chosen_type = _pick_provider_type(entry, provider_type)
    rendered = _format_templates(entry.get("metadata_url_templates") or {}, string_vars)

    # Prefer PingFederate discovery when base_url is set without env_id.
    if idp_id == "ping" and string_vars.get("base_url") and not string_vars.get("env_id"):
        base = string_vars["base_url"].rstrip("/")
        rendered = {
            "issuer": base,
            "oidc_discovery": f"{base}/.well-known/openid-configuration",
            "jwks_url": f"{base}/pf/JWKS",
            "scim_base_url": rendered.get(
                "scim_base_url", f"https://boss.example/scim/v2/{project_id}"
            ),
        }

    issuer = rendered.get("issuer")
    jwks_url = rendered.get("jwks_url")
    sso_url = rendered.get("sso_url") or rendered.get("authorize_url")
    entity_id = rendered.get("entity_id") or (
        issuer if chosen_type == "saml" else None
    )
    scim_base_url = rendered.get("scim_base_url") if chosen_type == "scim" else None
    # For multi-protocol packs, still surface scim URL in metadata for operators.
    resolved_secret_ref = (
        (secret_ref or string_vars.get("secret_ref") or "").strip() or None
    )
    resolved_audience = (
        (audience or string_vars.get("audience") or "").strip() or None
    )
    resolved_display = (
        (display_name or "").strip() or f"{entry['name']} ({chosen_type.upper()})"
    )

    enterprise_payload: dict[str, Any] = {
        "project_id": project_id,
        "provider_key": entry["id"],
        "provider_type": chosen_type if chosen_type in ("oidc", "saml", "scim") else "oidc",
        "display_name": resolved_display,
        "issuer": issuer,
        "audience": resolved_audience,
        "entity_id": entity_id if chosen_type == "saml" else None,
        "sso_url": sso_url if chosen_type == "saml" else None,
        "jwks_url": jwks_url if chosen_type in ("oidc", "saml") else None,
        "certificate_fingerprint": None,
        "scim_base_url": scim_base_url
        if chosen_type == "scim"
        else rendered.get("scim_base_url"),
        "secret_ref": resolved_secret_ref,
        "status": status if status in ("planned", "active", "blocked", "disabled") else "planned",
        "test_status": "not_tested",
        "metadata": {
            "catalog_id": entry["id"],
            "catalog_name": entry["name"],
            "protocol_support": list(entry["protocol_support"]),
            "claim_map": dict(entry["default_oidc_claim_map"]),
            "rendered_endpoints": rendered,
            "docs_url": entry["docs_url"],
            "variables_used": sorted(string_vars.keys()),
            "dry_run": True,
        },
        "actor": actor,
    }

    # Strip None values from the upsert-shaped payload for clarity.
    upsert_body = {
        key: value
        for key, value in enterprise_payload.items()
        if value is not None
    }

    return {
        "dry_run": True,
        "idp_id": entry["id"],
        "idp_name": entry["name"],
        "project_id": project_id,
        "provider_type": enterprise_payload["provider_type"],
        "setup_steps": list(entry["setup_steps"]),
        "default_oidc_claim_map": dict(entry["default_oidc_claim_map"]),
        "rendered_endpoints": rendered,
        "docs_url": entry["docs_url"],
        "enterprise_upsert_path": "/v1/enterprise/identity-providers",
        "enterprise_payload": upsert_body,
        "notes": [
            "This blueprint is dry-run only; BOSS does not call the IdP or store secrets.",
            "Review enterprise_payload, store credentials via secret_ref, then POST to "
            "enterprise_upsert_path when ready.",
            "Activate only after a successful login/provisioning test.",
        ],
    }


def match_configured_providers(
    catalog_ids: Optional[list[str]],
    configured: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Correlate enterprise store records with catalog entries.

    ``configured`` items come from ``EnterpriseService.list_identity_providers``.
    Secrets are never included — only secret_ref names if present.
    """
    ids = catalog_ids or [e["id"] for e in IDENTITY_PROVIDER_CATALOG]
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in configured:
        key = str(row.get("provider_key") or "").strip().lower()
        if not key:
            continue
        by_key.setdefault(key, []).append(row)

    results: list[dict[str, Any]] = []
    for idp_id in ids:
        entry = _CATALOG_BY_ID.get(idp_id)
        rows = by_key.get(idp_id, [])
        safe_rows = [
            {
                "id": r.get("id"),
                "provider_key": r.get("provider_key"),
                "provider_type": r.get("provider_type"),
                "display_name": r.get("display_name"),
                "status": r.get("status"),
                "test_status": r.get("test_status"),
                "issuer": r.get("issuer"),
                "blocking_gaps": list(r.get("blocking_gaps") or []),
                "secret_ref": r.get("secret_ref"),  # name only, not secret value
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ]
        results.append(
            {
                "idp_id": idp_id,
                "name": entry["name"] if entry else idp_id,
                "catalog_status": entry["status"] if entry else "unknown",
                "configured": len(safe_rows) > 0,
                "configurations": safe_rows,
            }
        )
    return results
