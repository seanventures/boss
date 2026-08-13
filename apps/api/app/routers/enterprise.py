from __future__ import annotations

from fastapi import APIRouter


enterprise_router = APIRouter()

ENTERPRISE_EDITIONS = {
    "cloud",
    "enterprise",
    "enterprise_cloud",
    "enterprise_self_hosted",
}

# Paid Cloud/Enterprise *operational trust* apparatus only.
# Community self-hosters get BYOK + ProviderOps + **self-hosted SSO** (SAML/OIDC/SCIM).
_ENTERPRISE_PREFIXES = (
    "/v1/account-connect",
    "/v1/marketplace",
    "/v1/self-improvement",
    "/v1/enterprise",
)

# Mounted on Community (and paid). Self-hosted IdP/SSO — not hosted Account Connect.
_COMMUNITY_SSO_PREFIXES = (
    "/saml/",
    "/scim/",
    "/v1/enterprise/identity-providers",
    "/v1/enterprise/identity-rollout-policies",
    "/v1/enterprise/scim/",
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
# SSO (SAML SP, SCIM 2.0, IdP registry/rollout) is Community for self-hosted IdPs.
# What stays paid: hosted Account Connect, marketplace, enterprise cluster/compliance
# packs, and self-improvement automation. Deploy/delivery was already community.


def is_enterprise_path(path: str) -> bool:
    """Return whether a control-plane route belongs to the paid surface."""

    # Self-hosted SSO is Community (must be checked before /v1/enterprise catch-all).
    if path.startswith(_COMMUNITY_SSO_PREFIXES):
        return False
    if path.startswith(_ENTERPRISE_PREFIXES):
        return True
    return path.startswith("/v1/projects/") and "terraform-" in path
