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
)

# Community is fully functional for self-hosters who bring their own keys and manage
# their own infrastructure. That means the whole ProviderOps kernel is free:
# catalog, provider accounts (model + infra API keys), inventory sync, ChangeSets
# (plan/approve/apply with receipts), worker pools, autoscaler, and resource janitor.
#
# Gating ChangeSets while leaving keys free left community able to *store* DO/CF
# tokens but unable to *actuate* them through BOSS — half a workflow, same class of
# bug as half-gated delivery targets.
#
# What stays paid is Cloud/Enterprise *operational trust* apparatus:
# hosted Account Connect, marketplace, SSO/SCIM, enterprise cluster/compliance packs,
# and self-improvement automation. Deploy/delivery was already community.


def is_enterprise_path(path: str) -> bool:
    """Return whether a control-plane route belongs to the paid surface."""

    if path.startswith(_ENTERPRISE_PREFIXES):
        return True
    return path.startswith("/v1/projects/") and "terraform-" in path
