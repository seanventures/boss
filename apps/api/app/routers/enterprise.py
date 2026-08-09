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
)

# Deploying is COMMUNITY.
#
# `/v1/deployments` and `/v1/deployment-` used to sit in the list above, which meant every
# target CRUD, bootstrap, teardown, purge and auto-deploy route 404'd on a community control
# plane. The effect was not "a paid feature is unavailable" — it was a CI/CD console that
# rendered in full and failed on every button, because the deploy and health routes are
# community while everything needed to REGISTER a destination was not. A product cannot ship
# half a workflow behind a licence and expect the other half to make sense.
#
# The whole delivery family is now free: targets, runs, environment policies and promotions.
# Deploying what BOSS builds is the point of BOSS; the paid surface is the enterprise
# apparatus around it (SSO, marketplace, resource plane, self-improvement).


def is_enterprise_path(path: str) -> bool:
    """Return whether a control-plane route belongs to the paid surface."""

    if path.startswith(_ENTERPRISE_PREFIXES):
        return True
    return path.startswith("/v1/projects/") and "terraform-" in path
