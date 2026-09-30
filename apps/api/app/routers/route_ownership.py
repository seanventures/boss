"""Edition route ownership shared by the Community and paid API mounts.

This module contains route policy only. Community startup must be able to
import it without importing a paid router implementation.
"""

ENTERPRISE_EDITIONS = {
    "cloud",
    "enterprise",
    "enterprise_cloud",
    "enterprise_self_hosted",
}

# Paid Cloud/Enterprise operational trust apparatus only. Community self-hosters
# retain BYOK, ProviderOps and self-hosted SSO (SAML/OIDC/SCIM).
_ENTERPRISE_PREFIXES = (
    "/v1/account-connect",
    "/v1/marketplace",
    "/v1/self-improvement",
    "/v1/enterprise",
)

_COMMUNITY_SSO_PREFIXES = (
    "/saml/",
    "/scim/",
    "/v1/enterprise/identity-providers",
    "/v1/enterprise/identity-rollout-policies",
    "/v1/enterprise/scim/",
)


def is_enterprise_path(path: str) -> bool:
    """Return whether a control-plane route belongs to the paid surface."""

    if path.startswith(_COMMUNITY_SSO_PREFIXES):
        return False
    if path.startswith(_ENTERPRISE_PREFIXES):
        return True
    return path.startswith("/v1/projects/") and "terraform-" in path
