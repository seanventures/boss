from __future__ import annotations

from fastapi import APIRouter


enterprise_router = APIRouter()

ENTERPRISE_EDITIONS = {
    "cloud",
    "enterprise",
    "enterprise_cloud",
    "enterprise_self_hosted",
}

_ENTERPRISE_PREFIXES = (
    "/saml/",
    "/scim/",
    "/v1/account-connect",
    "/v1/enterprise",
    "/v1/marketplace",
    "/v1/self-improvement",
    "/v1/provider-definitions",
    "/v1/providers",
    "/v1/resources",
    "/v1/changesets",
    "/v1/resource-receipts",
    "/v1/worker-pools",
    "/v1/autoscaler",
    "/v1/resource-janitor",
    "/v1/deployments",
    "/v1/deployment-",
)


def is_enterprise_path(path: str) -> bool:
    """Return whether a control-plane route belongs to the paid surface."""

    if path.startswith(_ENTERPRISE_PREFIXES):
        return True
    return path.startswith("/v1/projects/") and "terraform-" in path
